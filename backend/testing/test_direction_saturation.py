"""Tests for the forced-strategist-disposition mechanism on saturated directions.

The behavior under test: a direction whose research keeps producing findings but
spawns no work is "saturated" — the prior outcome was a silent longer refractory
wait plus an idle direction. The saturation escalation in
``app/direction_saturation`` replaces that silent back-off with a durable
disposition item the strategist MUST answer (commission work / narrow scope /
retire), surfaced in the strategic payload every cycle until resolved.

Key invariants locked in here:

  * A saturated-but-still-active direction produces a persistent
    ``saturation_disposition.status == "required"`` item via
    ``apply_post_spawn_state`` (the findings-but-no-work path).
  * The item is NOT silently dropped across cycles and is NOT re-emitted (one
    durable item per saturated direction — re-flagging only happens after a
    genuine resolution).
  * A USER_PAUSED direction is NEVER escalated.
  * Terminal (expired/retired/completed/superseded) directions are never
    escalated.
  * The item surfaces through ``build_saturation_dispositions`` (the payload
    builder the strategist reads) with the bounded disposition vocabulary.
"""

from datetime import timedelta

import pytest

from app.time_utils import _utcnow
from app.models import Project, Task, TaskStatus
from test_autonomous_research import _active_direction, _make_research_task


# ── Helpers ──────────────────────────────────────────────────────────────────


def _history_entry(
    task_id: str,
    *,
    hours_ago: int,
    tasks_created: int,
    prs_merged: int = 0,
    scope_empty_noop_count: int | None = None,
    dedup: int = 0,
) -> dict:
    """One research_history entry shaped like the real ``apply_post_spawn_state``
    appends: counters + a filter_breakdown block."""
    entry = {
        "timestamp": (_utcnow() - timedelta(hours=hours_ago)).isoformat(),
        "task_id": task_id,
        "tasks_created": tasks_created,
        "prs_merged": prs_merged,
        "filter_breakdown": {
            "hash_dedup": dedup,
            "word_overlap_dedup": 0,
            "file_overlap_dedup": 0,
            "evidence_overlap_dedup": 0,
            "type_filtered": 0,
            "empty_prompt": 0,
        },
        "summary": "cycle",
    }
    if scope_empty_noop_count is not None:
        entry["scope_empty_noop_count"] = scope_empty_noop_count
    return entry


def _saturated_history() -> list[dict]:
    """3 genuine cycles all producing <=1 task (saturated window), with a recent
    merge inside the rolling 5-entry window so the direction is NOT genuinely
    exhausted (still shipping output) — the exact "saturated but not retired"
    signature the escalation targets."""
    return [
        _history_entry("t-sat-1", hours_ago=15, tasks_created=1, prs_merged=1),
        _history_entry("t-sat-2", hours_ago=10, tasks_created=1),
        _history_entry("t-sat-3", hours_ago=5, tasks_created=0),
    ]


async def _run_post_spawn_cycle(
    db_session,
    project: Project,
    task: Task,
    direction_id: str,
) -> None:
    """Run one findings-but-no-work research cycle against the direction."""
    from app.research_direction_state import apply_post_spawn_state

    await apply_post_spawn_state(
        db_session,
        task,
        [],  # findings
        [],  # child_tasks
        [],  # deduplicated
        [],  # insights
        [],  # direction_notes
        [],  # direction_proposals
        0, 0, 0, 0, 0, 0,
        "research-branch", "main", 0, None,
    )
    await db_session.refresh(project)


# ── flag_saturation_disposition: core unit behavior ──────────────────────────


def test_flag_saturation_disposition_creates_durable_item():
    from app.direction_saturation import (
        DISPOSITION_REQUIRED,
        SATURATION_DISPOSITION_KEY,
        flag_saturation_disposition,
    )

    direction = {**_active_direction("d-sat-unit"), "research_history": _saturated_history()}
    created = flag_saturation_disposition(
        direction,
        now_iso="2026-08-01T00:00:00Z",
        findings_produced=4,
        tasks_created=0,
    )
    assert created is True
    item = direction[SATURATION_DISPOSITION_KEY]
    assert item["status"] == DISPOSITION_REQUIRED
    assert item["flagged_at"] == "2026-08-01T00:00:00Z"
    assert item["findings_produced"] == 4
    assert item["recent_tasks_created"] == 0
    assert item["cycles_researched"] == 3
    assert "commission work" in item["reason"]


def test_flag_saturation_disposition_not_reemitted_while_pending():
    """One durable item per saturated direction — a second flag is a no-op."""
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        flag_saturation_disposition,
    )

    direction = {**_active_direction("d-sat-dedup"), "research_history": _saturated_history()}
    assert flag_saturation_disposition(
        direction, now_iso="2026-08-01T00:00:00Z", findings_produced=2, tasks_created=0
    ) is True
    assert flag_saturation_disposition(
        direction, now_iso="2026-08-02T00:00:00Z", findings_produced=3, tasks_created=0
    ) is False
    item = direction[SATURATION_DISPOSITION_KEY]
    # The original item is untouched — flagged_at stays at the first stamp.
    assert item["flagged_at"] == "2026-08-01T00:00:00Z"
    assert item["findings_produced"] == 2


def test_flag_saturation_disposition_skips_user_paused():
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        flag_saturation_disposition,
    )

    # USER_PAUSED = status "paused" and NOT auto_paused.
    paused = {
        **_active_direction("d-sat-paused"),
        "status": "paused",
        "research_history": _saturated_history(),
    }
    assert flag_saturation_disposition(
        paused, now_iso="2026-08-01T00:00:00Z", findings_produced=5, tasks_created=0
    ) is False
    assert SATURATION_DISPOSITION_KEY not in paused

    # Auto-paused directions are NOT user-paused — but for the escalation rule we
    # still only skip USER_PAUSED; auto-paused is not in scope for this unit.
    auto_paused = {
        **_active_direction("d-sat-autopaused"),
        "status": "paused",
        "auto_paused": True,
        "research_history": _saturated_history(),
    }
    assert flag_saturation_disposition(
        auto_paused, now_iso="2026-08-01T00:00:00Z", findings_produced=1, tasks_created=0
    ) is True
    assert SATURATION_DISPOSITION_KEY in auto_paused


def test_flag_saturation_disposition_skips_terminal_directions():
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        flag_saturation_disposition,
    )

    for status in ("expired", "retired", "completed", "superseded"):
        direction = {
            **_active_direction(f"d-sat-term-{status}"),
            "status": status,
            "research_history": _saturated_history(),
        }
        assert flag_saturation_disposition(
            direction,
            now_iso="2026-08-01T00:00:00Z",
            findings_produced=1,
            tasks_created=0,
        ) is False, f"terminal status {status} must not be escalated"
        assert SATURATION_DISPOSITION_KEY not in direction


def test_resolve_and_clear_saturation_disposition():
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        clear_saturation_disposition,
        flag_saturation_disposition,
        resolve_saturation_disposition,
    )

    direction = {**_active_direction("d-sat-resolve"), "research_history": _saturated_history()}
    assert flag_saturation_disposition(
        direction, now_iso="2026-08-01T00:00:00Z", findings_produced=2, tasks_created=0
    ) is True

    # resolve removes the item and reports it.
    assert resolve_saturation_disposition(
        direction, disposition="narrow_scope", resolved_at="2026-08-02T00:00:00Z"
    ) is True
    assert SATURATION_DISPOSITION_KEY not in direction
    # Resolving when no item is pending is a no-op.
    assert resolve_saturation_disposition(
        direction, disposition="retire", resolved_at="2026-08-03T00:00:00Z"
    ) is False

    # A resolved item lets a genuine NEW saturation episode re-flag cleanly.
    assert flag_saturation_disposition(
        direction, now_iso="2026-08-03T00:00:00Z", findings_produced=1, tasks_created=0
    ) is True

    # clear removes the item and reports it; clearing when absent is a no-op.
    assert clear_saturation_disposition(direction) is True
    assert SATURATION_DISPOSITION_KEY not in direction
    assert clear_saturation_disposition(direction) is False


def test_build_saturation_dispositions_surfaces_only_required():
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        build_saturation_dispositions,
        flag_saturation_disposition,
    )

    pending = {**_active_direction("d-sat-build"), "research_history": _saturated_history()}
    flag_saturation_disposition(
        pending, now_iso="2026-08-01T00:00:00Z", findings_produced=3, tasks_created=0
    )
    resolved = {**_active_direction("d-sat-build-resolved"), "research_history": _saturated_history()}
    flag_saturation_disposition(
        resolved, now_iso="2026-08-01T00:00:00Z", findings_produced=3, tasks_created=0
    )
    resolved[SATURATION_DISPOSITION_KEY] = {
        "status": "resolved",
        "flagged_at": "2026-08-01T00:00:00Z",
    }
    no_item = {**_active_direction("d-sat-build-clean"), "research_history": _saturated_history()}

    class _FakeProject:
        directions = [pending, resolved, no_item]

    items = build_saturation_dispositions(_FakeProject())  # type: ignore[arg-type]
    assert [i["id"] for i in items] == ["d-sat-build"]
    assert items[0]["findings_produced"] == 3
    assert items[0]["text"] == "Test direction"
    assert items[0]["priority"] == "medium"


def test_paused_after_flag_is_dormant_not_surfaced():
    """A direction flagged then operator-paused must not surface a forced
    disposition (the operator's pause answers it); the item is not dropped, so
    unpausing while still saturated resumes surfacing."""
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        build_saturation_dispositions,
        flag_saturation_disposition,
    )

    paused_later = {
        **_active_direction("d-sat-pause-after"),
        "research_history": _saturated_history(),
    }
    assert flag_saturation_disposition(
        paused_later, now_iso="2026-08-01T00:00:00Z", findings_produced=2, tasks_created=0
    ) is True
    assert SATURATION_DISPOSITION_KEY in paused_later

    # Operator pauses the flagged direction.
    paused_later["status"] = "paused"

    class _FakeProject:
        directions = [paused_later]

    assert build_saturation_dispositions(_FakeProject()) == []  # type: ignore[arg-type]
    # The item is dormant, not dropped — the direction still carries it.
    assert SATURATION_DISPOSITION_KEY in paused_later


# ── Integration: apply_post_spawn_state flags saturated directions ───────────


@pytest.mark.asyncio
async def test_saturated_direction_flagged_via_apply_post_spawn_state(db_session):
    """A direction that keeps researching but spawns no work (saturated, still
    active because it has recent merges) gets a persistent disposition item."""
    from app.direction_saturation import (
        DISPOSITION_REQUIRED,
        SATURATION_DISPOSITION_KEY,
        build_saturation_dispositions,
    )

    direction_id = "d-sat-int"
    direction = {
        **_active_direction(direction_id, "Saturated integration direction"),
        "research_history": _saturated_history(),
    }
    project = Project(
        name="Saturation Integration Test",
        user_id="u-sat-int",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    await _run_post_spawn_cycle(db_session, project, task, direction_id)

    d = project.directions[0]
    assert d["status"] == "active", "must remain active (recent merges → not exhausted)"
    assert d.get("possibly_saturated") is True, "direction must be saturated"
    item = d.get(SATURATION_DISPOSITION_KEY)
    assert item is not None, "saturated direction must carry a disposition item"
    assert item["status"] == DISPOSITION_REQUIRED

    # The item surfaces through the strategic-payload builder.
    surfaced = build_saturation_dispositions(project)
    assert [e["id"] for e in surfaced] == [direction_id]


@pytest.mark.asyncio
async def test_disposition_persists_and_is_not_duplicated_across_cycles(db_session):
    """Across repeated research cycles the pending item is neither dropped nor
    re-created: one durable item per saturated direction."""
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        build_saturation_dispositions,
    )

    direction_id = "d-sat-persist"
    direction = {
        **_active_direction(direction_id, "Saturation persistence direction"),
        "research_history": _saturated_history(),
    }
    project = Project(
        name="Saturation Persistence Test",
        user_id="u-sat-persist",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Cycle 1 → item created.
    await _run_post_spawn_cycle(db_session, project, task, direction_id)
    d = project.directions[0]
    item = d.get(SATURATION_DISPOSITION_KEY)
    assert item is not None
    first_flagged_at = item["flagged_at"]

    # Cycle 2 → the item persists (not silently dropped) and is NOT re-created
    # (not duplicated): still one item with the original flagged_at.
    await _run_post_spawn_cycle(db_session, project, task, direction_id)
    await db_session.refresh(project)
    d = project.directions[0]
    item = d.get(SATURATION_DISPOSITION_KEY)
    assert item is not None, "pending item must not be dropped across cycles"
    assert item["flagged_at"] == first_flagged_at, "pending item must not be re-created"

    # build_saturation_dispositions still surfaces exactly one entry.
    surfaced = build_saturation_dispositions(project)
    assert len(surfaced) == 1
    assert surfaced[0]["id"] == direction_id

    # Cycle 3 after resolving → a genuine NEW saturation episode re-flags cleanly.
    from app.direction_saturation import clear_saturation_disposition

    d = project.directions[0]
    assert clear_saturation_disposition(d) is True
    await db_session.refresh(project)
    await _run_post_spawn_cycle(db_session, project, task, direction_id)
    await db_session.refresh(project)
    d = project.directions[0]
    assert d.get(SATURATION_DISPOSITION_KEY) is not None, (
        "a fresh saturation episode after resolution must re-flag"
    )


@pytest.mark.asyncio
async def test_user_paused_direction_not_escalated_via_apply_post_spawn_state(db_session):
    """A USER_PAUSED direction with an identical saturated history is never
    escalated — the operator already made a decision about it."""
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        build_saturation_dispositions,
    )

    direction_id = "d-sat-user-paused"
    direction = {
        **_active_direction(direction_id, "User-paused saturated direction"),
        "status": "paused",  # USER_PAUSED: no auto_paused flag
        "research_history": _saturated_history(),
    }
    project = Project(
        name="Saturation User-Paused Test",
        user_id="u-sat-upaused",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    await _run_post_spawn_cycle(db_session, project, task, direction_id)

    d = project.directions[0]
    assert d["status"] == "paused"
    assert SATURATION_DISPOSITION_KEY not in d, (
        "USER_PAUSED direction must never carry a disposition item"
    )
    assert build_saturation_dispositions(project) == []


# ── Integration: strategic ops resolve a pending item ────────────────────────


@pytest.mark.asyncio
async def test_retire_direction_resolves_pending_saturation_item(db_session):
    """retire_direction (the 'retire' disposition) drops a pending saturation
    item so a resolved disposition is never re-surfaced."""
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        flag_saturation_disposition,
    )

    direction_id = "d-sat-retire-op"
    direction = {
        **_active_direction(direction_id, "Retire-op saturated direction"),
        "research_history": _saturated_history(),
    }
    flag_saturation_disposition(
        direction, now_iso=_utcnow().isoformat(), findings_produced=2, tasks_created=0
    )
    project = Project(
        name="Saturation Retire Op Test",
        user_id="u-sat-retire-op",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Route the strategist retire op through the real /api/agent/strategic
    # endpoint (task_type gating + challenge gate + locked direction write all
    # run for real).
    review = Task(
        project_id=project.id,
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)

    from httpx import ASGITransport, AsyncClient

    from app.database import get_session
    from app.main import app
    from app.routers.agent import verify_agent_token

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": str(review.id),
        "task_type": "strategic_review",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "retire_direction",
                    "task_id": str(review.id),
                    "data": {
                        "direction_id": direction_id,
                        "rationale": "No work left; retiring per forced disposition.",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "ok", body
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)

    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "expired"
    assert SATURATION_DISPOSITION_KEY not in d, (
        "retire_direction must drop the pending saturation item"
    )


# ── No spurious flags on healthy directions ──────────────────────────────────


@pytest.mark.asyncio
async def test_productive_direction_not_flagged_via_apply_post_spawn_state(db_session):
    """A direction that keeps spawning tasks (non-saturated) is never escalated."""
    from app.direction_saturation import (
        SATURATION_DISPOSITION_KEY,
        build_saturation_dispositions,
    )

    direction_id = "d-productive"
    productive_history = [
        _history_entry("t-prod-1", hours_ago=15, tasks_created=3, prs_merged=1),
        _history_entry("t-prod-2", hours_ago=10, tasks_created=2, prs_merged=1),
        _history_entry("t-prod-3", hours_ago=5, tasks_created=2),
    ]
    direction = {
        **_active_direction(direction_id, "Productive direction"),
        "research_history": productive_history,
    }
    project = Project(
        name="Productive Direction Test",
        user_id="u-prod",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    await _run_post_spawn_cycle(db_session, project, task, direction_id)

    d = project.directions[0]
    assert d.get("possibly_saturated") is False
    assert SATURATION_DISPOSITION_KEY not in d
    assert build_saturation_dispositions(project) == []
