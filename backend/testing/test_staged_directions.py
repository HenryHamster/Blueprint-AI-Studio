"""Tests for the staged-direction lifecycle + auto-activation spine.

Covers the three deliverables of the staging spine:

  1. A ``staged`` direction is INERT — it survives the lifecycle prune, is never
     returned as an eligible/active direction (so it spawns no research), and is
     never auto-paused / auto-completed.
  2. ``activate_eligible_staged_directions`` flips exactly the staged directions
     whose ``staged_for_version`` has already promoted, is idempotent, and does
     NOT activate work staged during the version that just opened.
  3. The reaper backstop (``sweep_activate_staged_directions``) activates a
     staged direction whose target version has already opened.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import release_version as rv
from app.auth import create_agent_token
from app.direction_lifecycle import _process_direction_lifecycle
from app.directions_repo import (
    activate_eligible_staged_directions,
    sync_directions,
)
from app.models import Alert, Direction, Project, ProjectMember, Task, TaskStatus
from app.reaper_sweeps import (
    sweep_activate_staged_directions,
    sweep_stale_staged_directions,
)
from app.strategic_spawning import _build_staged_directions_summary
from app.time_utils import _utcnow


def _make_noop():
    """An AsyncMock stand-in for broadcast_to_project (swallows the emit)."""
    from unittest.mock import AsyncMock

    return AsyncMock()


def _staged_direction(did: str, staged_for_version: int, **extra) -> dict:
    d = {
        "id": did,
        "text": f"Staged work {did}",
        "status": "staged",
        "staged_for_version": staged_for_version,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    d.update(extra)
    return d


def _active_direction(did: str, **extra) -> dict:
    d = {
        "id": did,
        "text": f"Active work {did}",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 1. A staged direction is inert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staged_direction_inert_in_lifecycle(db_session):
    """A staged direction survives the prune, is excluded from eligible/active
    directions (spawns no research), and is never auto-paused even when its
    frozen history would auto-pause an active direction."""
    # A research_history that would trip an unproductive/zero-merge auto-pause
    # if this direction were ACTIVE — proves staged is skipped, not evaluated.
    poisoned_history = [
        {
            "timestamp": "2026-01-01T00:00:00",
            "tasks_created": 0,
            "prs_merged": 0,
            "no_findings": True,
        }
        for _ in range(8)
    ]
    project = Project(
        name="Staged-Inert",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _staged_direction(
                "d-staged01",
                staged_for_version=2,
                research_history=poisoned_history,
                last_researched_at=(
                    datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=10)
                ).isoformat(),
            )
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active = await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=datetime.now(timezone.utc).replace(tzinfo=None)
    )

    # Excluded from active/eligible → spawns no research.
    assert active == [], "Staged direction must not be eligible for research"

    await db_session.refresh(project)
    statuses = {d["id"]: d["status"] for d in project.directions}
    # Survives the prune AND was not auto-paused/auto-completed.
    assert statuses == {"d-staged01": "staged"}


# ---------------------------------------------------------------------------
# 2. activate_eligible_staged_directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activation_helper_flips_eligible_only(db_session):
    """The helper activates exactly the staged directions whose target version
    has promoted, leaves work staged during the just-opened version staged, and
    is idempotent."""
    project = Project(
        name="Staged-Activate",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _staged_direction("d-elig", staged_for_version=2),   # 2 <= 3 → activate
            _staged_direction("d-bound", staged_for_version=3),  # 3 <= 3 → activate
            _staged_direction("d-next", staged_for_version=4),   # 4 > 3 → stays staged
            _active_direction("d-act"),                          # untouched
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=3
    )
    assert sorted(activated) == ["d-bound", "d-elig"]

    await db_session.refresh(project)
    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-elig"]["status"] == "active"
    assert by_id["d-bound"]["status"] == "active"
    # Marker cleared + activation stamped on the flipped ones.
    assert "staged_for_version" not in by_id["d-elig"]
    assert by_id["d-elig"]["staged_activated_at"]
    # Work staged DURING the version that just opened stays staged.
    assert by_id["d-next"]["status"] == "staged"
    assert by_id["d-next"]["staged_for_version"] == 4
    # Pre-existing active direction untouched.
    assert by_id["d-act"]["status"] == "active"

    # Idempotent: re-running activates nothing new (already-active is a no-op).
    again = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=3
    )
    assert again == []
    await db_session.refresh(project)
    assert {d["id"]: d["status"] for d in project.directions} == {
        "d-elig": "active",
        "d-bound": "active",
        "d-next": "staged",
        "d-act": "active",
    }


@pytest.mark.asyncio
async def test_activation_helper_restaffs_problem_model_gap(db_session):
    """Activating staged gap-linked work staffs its gap and clears deferral."""
    project = Project(
        name="Staged-Activate-Gap",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _staged_direction("d-gap", staged_for_version=2, gap_id="g-gap"),
        ],
        problem_model={
            "revision": 1,
            "gaps": [
                {
                    "id": "g-gap",
                    "name": "gap",
                    "status": "active",
                    "active_moves": [],
                    "past_moves": ["d-old"],
                    "deferral": {
                        "rationale": "intentionally staged",
                        "created_at": "2026-01-01T00:00:00",
                    },
                }
            ],
        },
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=2
    )

    assert activated == ["d-gap"]
    await db_session.refresh(project)
    gap = project.problem_model["gaps"][0]
    assert gap["active_moves"] == ["d-gap"]
    assert gap["past_moves"] == ["d-old"]
    assert gap["deferral"] is None


@pytest.mark.asyncio
async def test_activation_helper_fail_open_on_missing_marker(db_session):
    """A staged direction with no parseable staged_for_version is activated
    (fail-open) so a malformed marker can never strand work forever."""
    bad = _staged_direction("d-nomarker", staged_for_version=1)
    bad.pop("staged_for_version")  # no marker at all
    project = Project(
        name="Staged-FailOpen",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[bad],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    assert activated == ["d-nomarker"]
    await db_session.refresh(project)
    assert project.directions[0]["status"] == "active"


# ---------------------------------------------------------------------------
# 2b. Active-direction cap is a genuine ceiling across auto-activation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activation_respects_active_cap(db_session, monkeypatch):
    """Auto-activation never pushes the project past the active-direction cap:
    with N eligible staged directions but only ``slots`` free, exactly ``slots``
    flip and the remainder stay staged with their markers intact."""
    from app import shared_state

    monkeypatch.setitem(shared_state.runtime_config, "max_active_directions", 3)

    project = Project(
        name="Cap-Ceiling",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _active_direction("d-act1"),  # 2 active already → 1 free slot
            _active_direction("d-act2"),
            _staged_direction("d-s1", staged_for_version=1, staged_at="2026-01-01T00:00:00"),
            _staged_direction("d-s2", staged_for_version=1, staged_at="2026-01-02T00:00:00"),
            _staged_direction("d-s3", staged_for_version=1, staged_at="2026-01-03T00:00:00"),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    # Only one slot was free (cap 3, two already active) → exactly one flips.
    assert activated == ["d-s1"]

    await db_session.refresh(project)
    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-s1"]["status"] == "active"
    # Overflow stays staged with its target marker intact for the next sweep.
    for did in ("d-s2", "d-s3"):
        assert by_id[did]["status"] == "staged"
        assert by_id[did]["staged_for_version"] == 1
        assert "staged_activated_at" not in by_id[did]


@pytest.mark.asyncio
async def test_activation_at_cap_activates_none(db_session, monkeypatch):
    """Already at the cap: activation returns [] and every staged direction stays
    staged (no slot to flip into)."""
    from app import shared_state

    monkeypatch.setitem(shared_state.runtime_config, "max_active_directions", 2)

    project = Project(
        name="Cap-Full",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _active_direction("d-act1"),
            _active_direction("d-act2"),  # already at cap 2
            _staged_direction("d-s1", staged_for_version=1),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    assert activated == []

    await db_session.refresh(project)
    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-s1"]["status"] == "staged"
    assert by_id["d-s1"]["staged_for_version"] == 1


@pytest.mark.asyncio
async def test_activation_drains_overflow_across_sweeps(db_session, monkeypatch):
    """Overflow held back by the cap drains on a later sweep once an active slot
    frees — staged work is never permanently stranded."""
    from app import shared_state

    monkeypatch.setitem(shared_state.runtime_config, "max_active_directions", 2)

    project = Project(
        name="Cap-Drain",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _active_direction("d-act1"),  # 1 active → 1 free slot
            _staged_direction("d-s1", staged_for_version=1, staged_at="2026-01-01T00:00:00"),
            _staged_direction("d-s2", staged_for_version=1, staged_at="2026-01-02T00:00:00"),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First sweep: only one slot free → only the oldest staged direction flips.
    first = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    assert first == ["d-s1"]

    # Free an active slot (simulate a direction completing), then re-sweep.
    await db_session.refresh(project)

    def _complete_act1(directions):
        for d in directions:
            if d["id"] == "d-act1":
                d["status"] = "completed"

    from app.task_locking import with_directions_locked

    await with_directions_locked(db_session, project.id, _complete_act1)
    await db_session.commit()

    second = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    assert second == ["d-s2"]

    await db_session.refresh(project)
    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-s2"]["status"] == "active"


@pytest.mark.asyncio
async def test_activation_cap_ordering(db_session, monkeypatch):
    """When a single slot is contested, the higher-priority direction wins; among
    equal priority the older ``staged_at`` (FIFO) wins."""
    from app import shared_state

    monkeypatch.setitem(shared_state.runtime_config, "max_active_directions", 1)

    project = Project(
        name="Cap-Order",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            # No active directions → exactly one free slot.
            _staged_direction(
                "d-low", staged_for_version=1, priority="low",
                staged_at="2026-01-01T00:00:00",
            ),
            _staged_direction(
                "d-high", staged_for_version=1, priority="high",
                staged_at="2026-01-05T00:00:00",
            ),
            _staged_direction(
                "d-high-older", staged_for_version=1, priority="high",
                staged_at="2026-01-02T00:00:00",
            ),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    # High priority beats low; among the two high, the older staged_at wins.
    assert activated == ["d-high-older"]

    await db_session.refresh(project)
    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-high-older"]["status"] == "active"
    assert by_id["d-high"]["status"] == "staged"
    assert by_id["d-low"]["status"] == "staged"


# ---------------------------------------------------------------------------
# 3. Reaper backstop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_backstop_activates_promoted_target(db_session):
    """The reaper sweep activates a staged direction whose target version has
    already promoted (current open version is higher), while leaving a direction
    staged for the still-open version staged."""
    project = Project(
        name="Staged-Reaper",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _staged_direction("d-ready", staged_for_version=1),  # v1 promoted → activate
            _staged_direction("d-hold", staged_for_version=2),   # v2 still open → stays staged
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Populate the normalised direction table the sweep prefilters on.
    await sync_directions(db_session, project.id, project.directions)
    terminal = await rv.open_version(db_session, project.id, version_number=1)
    terminal.status = "promoted"
    await db_session.commit()
    # Version 2 is OPEN while v1 is canonically terminal.
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()

    total = await sweep_activate_staged_directions(db_session)
    assert total == 1

    await db_session.refresh(project)
    by_id = {d["id"]: d["status"] for d in project.directions}
    assert by_id["d-ready"] == "active"
    assert by_id["d-hold"] == "staged"
    notes = next(d for d in project.directions if d["id"] == "d-ready")["notes"]
    assert "v1 promoted" in notes[-1]["text"]
    messages = await _activation_messages(db_session, project.id)
    assert messages[-1].context["terminal_version_number"] == 1
    assert messages[-1].context["terminal_status"] == "promoted"

    # Idempotent: a second sweep activates nothing more.
    assert await sweep_activate_staged_directions(db_session) == 0


@pytest.mark.asyncio
async def test_reaper_backstop_noop_without_open_version(db_session):
    """With no promoted version yet (v1 still open), nothing is activated."""
    project = Project(
        name="Staged-Reaper-NoPromote",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-wait", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await sync_directions(db_session, project.id, project.directions)
    await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    total = await sweep_activate_staged_directions(db_session)
    assert total == 0
    await db_session.refresh(project)
    assert project.directions[0]["status"] == "staged"

    # Sanity: the prefilter did find the project (it has a staged Direction row).
    rows = (
        (
            await db_session.execute(
                select(Direction.project_id).where(Direction.status == "staged")
            )
        )
        .scalars()
        .all()
    )
    assert project.id in rows


@pytest.mark.asyncio
async def test_reaper_backstop_requires_canonical_preceding_terminal_row(db_session):
    """A higher open version alone is not proof that its predecessor closed."""
    project = Project(
        name="Staged-Reaper-Missing-Terminal",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-missing-terminal", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await sync_directions(db_session, project.id, project.directions)
    # Deliberately model a damaged/imported history: v2 is open but no
    # canonical promoted/abandoned v1 exists. The reaper must fail closed.
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()

    assert await sweep_activate_staged_directions(db_session) == 0
    await db_session.refresh(project)
    assert project.directions[0]["status"] == "staged"
    assert await _activation_messages(db_session, project.id) == []


@pytest.mark.asyncio
async def test_reaper_backstop_reports_abandoned_terminal_status(db_session):
    project = Project(
        name="Staged-Reaper-Abandoned",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-abandoned", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await sync_directions(db_session, project.id, project.directions)
    terminal = await rv.open_version(db_session, project.id, version_number=1)
    terminal.status = "abandoned"
    await db_session.commit()
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()

    assert await sweep_activate_staged_directions(db_session) == 1
    await db_session.refresh(project)
    notes = project.directions[0]["notes"]
    assert "v1 abandoned" in notes[-1]["text"]
    messages = await _activation_messages(db_session, project.id)
    assert "v1 abandoned" in messages[-1].content
    assert messages[-1].context["terminal_version_number"] == 1
    assert messages[-1].context["terminal_status"] == "abandoned"


# ---------------------------------------------------------------------------
# 3b. The reaper staleness sweep alerts on stalled staged work
# ---------------------------------------------------------------------------


async def _stale_project(db_session, directions):
    project = Project(
        name="Staged-Stale",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=directions,
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await sync_directions(db_session, project.id, project.directions)
    # v2 is open → v2-staged work has NOT promoted (target version still open).
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()
    return project


async def _stale_alerts(db_session, project_id):
    rows = (
        await db_session.execute(
            select(Alert).where(Alert.project_id == project_id)
        )
    ).scalars().all()
    return [a for a in rows if a.title.startswith("Stale staged direction")]


@pytest.mark.asyncio
async def test_stale_sweep_alerts_on_long_staged(db_session, monkeypatch):
    """A staged direction waiting past the threshold (target version still open)
    fires exactly one alert; a second sweep dedups."""
    monkeypatch.setattr(
        "app.alert_dispatch.broadcast_to_project", _make_noop()
    )
    old = (_utcnow() - timedelta(days=30)).isoformat()
    project = await _stale_project(
        db_session,
        [_staged_direction("d-stale", staged_for_version=2, staged_at=old)],
    )

    assert await sweep_stale_staged_directions(db_session) == 1
    alerts = await _stale_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert "d-stale" in alerts[0].title
    assert "30 day" in alerts[0].message

    # Idempotent: a second sweep raises no duplicate.
    assert await sweep_stale_staged_directions(db_session) == 0
    assert len(await _stale_alerts(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_stale_sweep_ignores_fresh_staged(db_session, monkeypatch):
    """A freshly staged direction (recent timestamp) is NOT flagged."""
    monkeypatch.setattr(
        "app.alert_dispatch.broadcast_to_project", _make_noop()
    )
    recent = (_utcnow() - timedelta(days=1)).isoformat()
    project = await _stale_project(
        db_session,
        [_staged_direction("d-fresh", staged_for_version=2, staged_at=recent)],
    )
    assert await sweep_stale_staged_directions(db_session) == 0
    assert await _stale_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_stale_sweep_fail_safe_when_timestamp_missing(
    db_session, monkeypatch
):
    """A staged direction with NO staging timestamp is fail-safe-flagged via the
    fallback age (default 21 >= threshold 21)."""
    monkeypatch.setattr(
        "app.alert_dispatch.broadcast_to_project", _make_noop()
    )
    d = _staged_direction("d-nots", staged_for_version=2)
    d.pop("staged_at", None)  # no staged_at at all
    project = await _stale_project(db_session, [d])
    assert await sweep_stale_staged_directions(db_session) == 1
    assert len(await _stale_alerts(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_stale_sweep_skips_already_promoted_target(
    db_session, monkeypatch
):
    """A staged direction whose target version has ALREADY promoted fires NO
    stale alert — that is the activation path's job, not staleness."""
    monkeypatch.setattr(
        "app.alert_dispatch.broadcast_to_project", _make_noop()
    )
    old = (_utcnow() - timedelta(days=30)).isoformat()
    # target v1 has promoted (open is v2) → not stalled, eligible for activation.
    project = await _stale_project(
        db_session,
        [_staged_direction("d-promoted", staged_for_version=1, staged_at=old)],
    )
    assert await sweep_stale_staged_directions(db_session) == 0
    assert await _stale_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_stale_sweep_respects_threshold_config(db_session, monkeypatch):
    """The staleness threshold is read from runtime config: raising it above the
    waited age suppresses the alert."""
    monkeypatch.setattr(
        "app.alert_dispatch.broadcast_to_project", _make_noop()
    )
    monkeypatch.setitem(
        __import__("app.shared_state", fromlist=["runtime_config"]).runtime_config,
        "staged_direction_stale_alert_days",
        90,
    )
    old = (_utcnow() - timedelta(days=30)).isoformat()
    project = await _stale_project(
        db_session,
        [_staged_direction("d-young", staged_for_version=2, staged_at=old)],
    )
    assert await sweep_stale_staged_directions(db_session) == 0
    assert await _stale_alerts(db_session, project.id) == []


# ---------------------------------------------------------------------------
# 4. The stage_direction thinker op (HTTP) round-trips through the mirror table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_direction_op_round_trips(client: AsyncClient, db_session):
    """POST stage_direction flips an active direction to staged, stamps the open
    version_number, and the change round-trips through the normalized Direction
    mirror table (the locked dual-write path)."""
    project = Project(
        name="Stage-Op",
        user_id="stage-user",
        autonomous_enabled=True,
        directions=[_active_direction("d-next-ver")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open version 5 — staging should stamp staged_for_version=5.
    await rv.open_version(db_session, project.id, version_number=5)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-stage-op",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-stage-op", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "stage_direction",
            "data": {
                "direction_id": "d-next-ver",
                "rationale": "build-phase idea — belongs to the next version",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "stage_direction"
    assert body["staged_for_version"] == 5

    # Blob reflects the staged status + marker.
    await db_session.refresh(project)
    staged = project.directions[0]
    assert staged["status"] == "staged"
    assert staged["staged_for_version"] == 5
    assert "staged_at" in staged

    # Round-trip: the normalized Direction mirror row is dual-written too.
    row = (
        await db_session.execute(
            select(Direction).where(
                Direction.project_id == project.id,
                Direction.direction_key == "d-next-ver",
            )
        )
    ).scalar_one()
    assert row.status == "staged"
    assert row.data["status"] == "staged"
    assert row.data["staged_for_version"] == 5


@pytest.mark.asyncio
async def test_stage_direction_op_no_open_version_skips(client: AsyncClient, db_session):
    """With no open version there is no target to auto-activate against — the op
    is skipped (409) and the direction is left untouched."""
    project = Project(
        name="Stage-Op-NoVersion",
        user_id="stage-user",
        autonomous_enabled=True,
        directions=[_active_direction("d-keep")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-stage-nover",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-stage-nover", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "stage_direction",
            "data": {"direction_id": "d-keep"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["status"] == "skipped"

    await db_session.refresh(project)
    assert project.directions[0]["status"] == "active"


# ---------------------------------------------------------------------------
# 5. The strategic payload surfaces staged directions
# ---------------------------------------------------------------------------


def test_build_staged_directions_summary_filters_and_shapes():
    """Only staged directions are surfaced, each as {id,text,priority,
    staged_for_version, staged_at, staged_age_days}; active/paused/proposed are
    excluded."""
    project = Project(
        name="Staged-Payload",
        user_id="stage-user",
        directions=[
            _active_direction("d-active"),
            _staged_direction("d-staged-2", staged_for_version=2, priority="high"),
            {"id": "d-paused", "status": "paused", "text": "paused work"},
            _staged_direction("d-staged-3", staged_for_version=3),
        ],
    )
    summary = _build_staged_directions_summary(project)
    by_id = {e["id"]: e for e in summary}
    assert set(by_id) == {"d-staged-2", "d-staged-3"}
    assert by_id["d-staged-2"]["staged_for_version"] == 2
    assert by_id["d-staged-2"]["priority"] == "high"
    assert by_id["d-staged-3"]["staged_for_version"] == 3
    assert by_id["d-staged-2"]["text"] == "Staged work d-staged-2"
    # Additive waiting-age fields present on every entry; None when no timestamp.
    assert by_id["d-staged-2"]["staged_at"] is None
    assert by_id["d-staged-2"]["staged_age_days"] is None


def test_build_staged_directions_summary_includes_staged_age():
    """A staged direction with a staging timestamp surfaces a derived
    ``staged_age_days`` so the strategist sees how long it has waited."""
    staged_at = (_utcnow() - timedelta(days=30)).isoformat()
    project = Project(
        name="Staged-Age",
        user_id="stage-user",
        directions=[
            _staged_direction("d-old", staged_for_version=2, staged_at=staged_at),
        ],
    )
    summary = _build_staged_directions_summary(project)
    assert len(summary) == 1
    entry = summary[0]
    assert entry["staged_at"] == staged_at
    assert entry["staged_age_days"] == 30


def test_build_staged_directions_summary_empty_when_none_staged():
    """No staged directions → empty list so the payload omits the field."""
    project = Project(
        name="No-Staged",
        user_id="stage-user",
        directions=[_active_direction("d-a"), _active_direction("d-b")],
    )
    assert _build_staged_directions_summary(project) == []


# ---------------------------------------------------------------------------
# 6. Auto-activation notifies the operator + annotates the direction
# ---------------------------------------------------------------------------


async def _activation_messages(db_session, project_id):
    from app.models import ProjectMessage

    rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id
            )
        )
    ).scalars().all()
    return [
        m
        for m in rows
        if (m.context or {}).get("event_type") == "direction_staged_activated"
    ]


@pytest.mark.asyncio
async def test_activation_annotates_direction_with_note(db_session):
    """Each activated direction gets a note naming the promoting version,
    symmetric with the note written when it was staged."""
    project = Project(
        name="Staged-Notes",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-note", staged_for_version=2)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=3
    )
    assert activated == ["d-note"]

    await db_session.refresh(project)
    notes = project.directions[0].get("notes") or []
    assert notes, "activation must append a note to the direction"
    assert "v3 promoted" in notes[-1]["text"]
    assert "resumed" in notes[-1]["text"].lower()
    assert notes[-1]["added_at"]


@pytest.mark.asyncio
async def test_activation_posts_operator_message(db_session):
    """Activation posts a single high-signal ProjectMessage naming the version
    and the activated count."""
    project = Project(
        name="Staged-Notify",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _staged_direction("d-n1", staged_for_version=2),
            _staged_direction("d-n2", staged_for_version=2),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=2
    )

    msgs = await _activation_messages(db_session, project.id)
    assert len(msgs) == 1
    msg = msgs[0]
    assert "v2" in msg.content
    assert (msg.context or {}).get("count") == 2
    assert (msg.context or {}).get("terminal_version_number") == 2
    assert (msg.context or {}).get("terminal_status") == "promoted"
    assert set((msg.context or {}).get("direction_ids") or []) == {"d-n1", "d-n2"}


@pytest.mark.asyncio
async def test_activation_no_message_when_nothing_activates(db_session):
    """A sweep that activates nothing posts no note and no message — and a
    repeat run is idempotent (no duplicate signal)."""
    project = Project(
        name="Staged-Idempotent",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-idem", staged_for_version=5)],  # 5 > 2
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Nothing eligible — no message at all.
    assert await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=2
    ) == []
    assert await _activation_messages(db_session, project.id) == []

    # Now activate, then re-run: the second run is a no-op and posts no
    # duplicate message.
    await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=5
    )
    assert len(await _activation_messages(db_session, project.id)) == 1
    await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=5
    )
    assert len(await _activation_messages(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_activation_survives_notification_failure(db_session, monkeypatch):
    """A notification failure is swallowed — the activation (status flip + note)
    must still commit, proving the best-effort isolation contract."""
    import app.notification_dispatch as nd

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(nd, "post_staged_directions_activated_message", _boom)

    project = Project(
        name="Staged-NotifyFail",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-fail", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    # Activation is unaffected by the notification failure.
    assert activated == ["d-fail"]
    await db_session.refresh(project)
    assert project.directions[0]["status"] == "active"
    assert project.directions[0].get("notes"), "note still written despite notify failure"


@pytest.mark.asyncio
async def test_activation_survives_deferred_flush_failure_after_commit(
    db_session,
    monkeypatch,
):
    """A post-commit external delivery failure cannot hide durable activation."""
    from unittest.mock import AsyncMock

    import app.notification_dispatch as nd
    import app.strategic_events as strategic_events

    def _boom(_session):
        raise RuntimeError("external scheduler down")

    emitted = AsyncMock()
    monkeypatch.setattr(nd, "flush_deferred_external_notifications", _boom)
    monkeypatch.setattr(strategic_events, "emit_strategic_updated", emitted)
    project = Project(
        name="Staged-FlushFail",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-flush", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )

    assert activated == ["d-flush"]
    await db_session.refresh(project)
    assert project.directions[0]["status"] == "active"
    assert project.directions[0].get("notes")
    assert len(await _activation_messages(db_session, project.id)) == 1
    assert "_deferred_external_notifications" not in db_session.info
    emitted.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6b. Activation resolves the open stale-staged alert when the stall clears.
# ---------------------------------------------------------------------------


async def _open_stale_alert(db_session, project_id, direction_id: str) -> Alert:
    """Create the WARNING alert the reaper raises for a stalled staged direction,
    using the exact same helper (so the deterministic title is byte-identical)."""
    from unittest.mock import AsyncMock
    import app.alert_dispatch as ad

    # Patch the broadcast so creation doesn't touch the websocket layer.
    orig = ad.broadcast_to_project
    ad.broadcast_to_project = AsyncMock()  # type: ignore[assignment]
    try:
        created = await ad._maybe_create_stale_staged_direction_alert(
            db_session,
            project_id,
            direction_id=direction_id,
            direction_text="stalled staged work",
            waited_days=30,
            staged_for_version=1,
            open_version=2,
        )
    finally:
        ad.broadcast_to_project = orig  # type: ignore[assignment]
    assert created is True
    alerts = await _stale_alerts(db_session, project_id)
    match = [a for a in alerts if direction_id in a.title]
    assert len(match) == 1 and match[0].dismissed_at is None
    return match[0]


@pytest.mark.asyncio
async def test_activation_resolves_open_stale_alert(db_session, monkeypatch):
    """When a staged direction auto-activates, its open stale-staged alert is
    dismissed — and only that direction's alert."""
    monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", _make_noop())
    project = Project(
        name="Staged-ResolveAlert",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[
            _staged_direction("d-stalled", staged_for_version=1),  # will activate
            _staged_direction("d-other", staged_for_version=5),    # stays staged
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two open stale alerts — one for each direction.
    alert_stalled = await _open_stale_alert(db_session, project.id, "d-stalled")
    alert_other = await _open_stale_alert(db_session, project.id, "d-other")

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    assert activated == ["d-stalled"]

    await db_session.refresh(alert_stalled)
    await db_session.refresh(alert_other)
    # The activated direction's alert is resolved; the still-staged one is not.
    assert alert_stalled.dismissed_at is not None
    assert alert_other.dismissed_at is None


@pytest.mark.asyncio
async def test_activation_resolves_stale_alert_via_reaper_backstop(
    db_session, monkeypatch
):
    """The reaper backstop shares the activation path, so it too resolves the
    open stale alert when it activates the direction."""
    monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", _make_noop())
    project = Project(
        name="Staged-ResolveViaReaper",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-reaper", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await sync_directions(db_session, project.id, project.directions)
    terminal = await rv.open_version(db_session, project.id, version_number=1)
    terminal.status = "promoted"
    await db_session.commit()
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()

    alert = await _open_stale_alert(db_session, project.id, "d-reaper")

    assert await sweep_activate_staged_directions(db_session) == 1
    await db_session.refresh(alert)
    assert alert.dismissed_at is not None


@pytest.mark.asyncio
async def test_activation_alert_resolution_idempotent(db_session, monkeypatch):
    """Resolving twice is a safe no-op: a second resolver call finds nothing
    open to dismiss and the dismissal timestamp is unchanged."""
    monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", _make_noop())
    from app.alert_dispatch import _resolve_stale_staged_direction_alerts

    project = Project(
        name="Staged-ResolveIdempotent",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-idem", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    alert = await _open_stale_alert(db_session, project.id, "d-idem")

    first = await _resolve_stale_staged_direction_alerts(
        db_session, project.id, ["d-idem"]
    )
    assert first == 1
    await db_session.refresh(alert)
    first_stamp = alert.dismissed_at
    assert first_stamp is not None

    # Second call: alert already dismissed → no match, no change.
    second = await _resolve_stale_staged_direction_alerts(
        db_session, project.id, ["d-idem"]
    )
    assert second == 0
    await db_session.refresh(alert)
    assert alert.dismissed_at == first_stamp


@pytest.mark.asyncio
async def test_activation_alert_resolution_noop_when_absent(db_session, monkeypatch):
    """Activation with no pre-existing stale alert dismisses nothing and does not
    error — the resolver is a safe no-op."""
    monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", _make_noop())
    from app.alert_dispatch import _resolve_stale_staged_direction_alerts

    project = Project(
        name="Staged-ResolveNoop",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-clean", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No alert exists — activation still works and resolves nothing.
    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    assert activated == ["d-clean"]
    assert await _stale_alerts(db_session, project.id) == []
    # Direct resolver call with no matching alert returns 0.
    assert (
        await _resolve_stale_staged_direction_alerts(
            db_session, project.id, ["d-clean"]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_activation_survives_alert_resolution_failure(db_session, monkeypatch):
    """If alert resolution raises, the activation (status flip) must still commit —
    proving the best-effort isolation contract."""
    monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", _make_noop())
    import app.alert_dispatch as ad

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("alert backend down")

    monkeypatch.setattr(ad, "_resolve_stale_staged_direction_alerts", _boom)

    project = Project(
        name="Staged-ResolveFail",
        user_id="staged-user",
        autonomous_enabled=True,
        directions=[_staged_direction("d-rfail", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    activated = await activate_eligible_staged_directions(
        db_session, project.id, promoted_version_number=1
    )
    assert activated == ["d-rfail"]
    await db_session.refresh(project)
    assert project.directions[0]["status"] == "active"


# ---------------------------------------------------------------------------
# 7. Atomic create-staged: a next-version direction is born staged in one op
#    (closes the create→stage race where a brand-new active direction is
#    immediately research-eligible).
# ---------------------------------------------------------------------------


async def _strategic_task(db_session, project_id, vm: str) -> Task:
    task = Task(
        project_id=project_id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id=vm,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


_CREATE_STAGED_DATA = {
    "text": "build-phase idea that belongs to the next version",
    "priority": "high",
    "success_evidence": "observable outcome that proves it is done",
    "retirement_condition": "measurable condition that triggers retirement",
}


@pytest.mark.asyncio
async def test_create_direction_op_staged_round_trips(client: AsyncClient, db_session):
    """create_direction with staged=true is born 'staged', stamps the open
    version, never passes through 'active', and the staged status round-trips
    through the normalized Direction mirror table."""
    project = Project(
        name="CreateStaged-Op",
        user_id="stage-user",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await rv.open_version(db_session, project.id, version_number=7)
    await db_session.commit()

    task = await _strategic_task(db_session, project.id, "agent-vm-create-staged")
    token = create_agent_token(
        str(task.id), "agent-vm-create-staged", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {**_CREATE_STAGED_DATA, "staged": True},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "create_direction"
    assert body["staged_for_version"] == 7
    new_id = body["direction_id"]

    await db_session.refresh(project)
    created = project.directions[0]
    assert created["id"] == new_id
    assert created["status"] == "staged"
    assert created["staged_for_version"] == 7
    assert "staged_at" in created

    # Round-trip: the normalized Direction mirror row reflects the staged status.
    row = (
        await db_session.execute(
            select(Direction).where(
                Direction.project_id == project.id,
                Direction.direction_key == new_id,
            )
        )
    ).scalar_one()
    assert row.status == "staged"
    assert row.data["staged_for_version"] == 7


@pytest.mark.asyncio
async def test_create_direction_op_staged_no_open_version_skips(
    client: AsyncClient, db_session
):
    """staged=true with no open version → 409 skipped and NOTHING is created
    (never silently falls back to an active direction)."""
    project = Project(
        name="CreateStaged-NoVersion",
        user_id="stage-user",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _strategic_task(db_session, project.id, "agent-vm-create-nover")
    token = create_agent_token(
        str(task.id), "agent-vm-create-nover", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {**_CREATE_STAGED_DATA, "staged": True},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["status"] == "skipped"

    # Nothing created — the blob is still empty.
    await db_session.refresh(project)
    assert project.directions == []


@pytest.mark.asyncio
async def test_create_direction_op_staged_ignores_active_cap(
    client: AsyncClient, db_session, monkeypatch
):
    """A staged create is inert, so it does NOT count against the active cap:
    creation succeeds even when active directions are already at the cap."""
    from app import shared_state

    monkeypatch.setitem(shared_state.runtime_config, "max_active_directions", 2)

    project = Project(
        name="CreateStaged-Cap",
        user_id="stage-user",
        autonomous_enabled=True,
        directions=[_active_direction("d-a1"), _active_direction("d-a2")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await rv.open_version(db_session, project.id, version_number=3)
    await db_session.commit()

    task = await _strategic_task(db_session, project.id, "agent-vm-create-cap")
    token = create_agent_token(
        str(task.id), "agent-vm-create-cap", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {**_CREATE_STAGED_DATA, "staged": True},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["staged_for_version"] == 3

    await db_session.refresh(project)
    statuses = sorted(d["status"] for d in project.directions)
    assert statuses == ["active", "active", "staged"]


# ---------------------------------------------------------------------------
# 8. Operator-facing creation path: add_direction with staged=true
# ---------------------------------------------------------------------------


async def _member_project(db_session, name: str, **extra) -> Project:
    """Create a project owned by the test client's current user."""
    project = Project(
        name=name,
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
        **extra,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_add_direction_staged_stamps_version(client: AsyncClient, db_session):
    """Operator add_direction with staged=true creates the direction directly in
    the staged state stamped to the open version."""
    project = await _member_project(db_session, "AddStaged")
    await rv.open_version(db_session, project.id, version_number=9)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project.id}/directions",
        json={
            "text": "operator next-version idea",
            "priority": "medium",
            "staged": True,
        },
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()["direction"]
    assert created["status"] == "staged"
    assert created["staged_for_version"] == 9
    assert "staged_at" in created

    await db_session.refresh(project)
    assert project.directions[0]["status"] == "staged"
    assert project.directions[0]["staged_for_version"] == 9


@pytest.mark.asyncio
async def test_add_direction_staged_no_open_version_404(
    client: AsyncClient, db_session
):
    """Operator add_direction with staged=true and no open version → 404 and no
    direction is created (never silently active)."""
    project = await _member_project(db_session, "AddStaged-NoVersion")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/directions",
        json={"text": "idea", "priority": "low", "staged": True},
    )
    assert resp.status_code == 404, resp.text

    await db_session.refresh(project)
    assert project.directions == []


@pytest.mark.asyncio
async def test_patch_direction_to_staged_stamps_staged_at(
    client: AsyncClient, db_session
):
    """Operator PATCH direction status to 'staged' records staged_at timestamp,
    symmetrically with the other staging paths (add_direction, agent create_direction,
    and agent stage_direction)."""
    project = await _member_project(db_session, "PatchStaged")
    project.directions = [_active_direction("d-patch-test")]
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await rv.open_version(db_session, project.id, version_number=4)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/directions/d-patch-test",
        json={"status": "staged"},
    )
    assert resp.status_code == 200, resp.text
    patched = resp.json()["direction"]
    assert patched["status"] == "staged"
    assert patched["staged_for_version"] == 4
    assert "staged_at" in patched, "PATCH to staged must record staged_at timestamp"

    await db_session.refresh(project)
    assert project.directions[0]["status"] == "staged"
    assert project.directions[0]["staged_for_version"] == 4
    assert "staged_at" in project.directions[0]


# ---------------------------------------------------------------------------
# 9. activate_direction strategist op
# ---------------------------------------------------------------------------


def _paused_direction(did: str, *, auto_paused: bool = True, **extra) -> dict:
    d = {
        "id": did,
        "text": f"Paused work {did}",
        "status": "paused",
        "auto_paused": auto_paused,
        "paused_at": "2026-01-01T00:00:00",
        "paused_reason": "stalled",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    d.update(extra)
    return d


@pytest.mark.asyncio
async def test_activate_direction_op_from_staged(client: AsyncClient, db_session):
    """POST activate_direction on a staged direction flips it to active and clears
    all staging markers (staged_for_version, staged_at)."""
    project = Project(
        name="Activate-Staged",
        user_id="activate-user",
        autonomous_enabled=True,
        directions=[
            _staged_direction(
                "d-activate-staged",
                staged_for_version=3,
                staged_at="2026-01-01T00:00:00",
            )
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _strategic_task(db_session, project.id, "agent-vm-activate-staged")
    token = create_agent_token(
        str(task.id), "agent-vm-activate-staged", task_type="strategic_review"
    )

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "activate_direction",
            "data": {
                "direction_id": "d-activate-staged",
                "rationale": "expediting to this version",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "activate_direction"
    assert body["direction_id"] == "d-activate-staged"

    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active"
    assert "staged_for_version" not in d, "staged_for_version must be cleared"
    assert "staged_at" not in d, "staged_at must be cleared"
    assert "staged_activated_at" not in d

    # Normalized mirror row reflects active status.
    row = (
        await db_session.execute(
            select(Direction).where(
                Direction.project_id == project.id,
                Direction.direction_key == "d-activate-staged",
            )
        )
    ).scalar_one()
    assert row.status == "active"


@pytest.mark.asyncio
async def test_activate_direction_op_from_paused(client: AsyncClient, db_session):
    """POST activate_direction on an auto-paused direction flips it to active and
    stamps auto_recovered_at so the direction gets a health-check grace window."""
    project = Project(
        name="Activate-Paused",
        user_id="activate-user2",
        autonomous_enabled=True,
        directions=[_paused_direction("d-activate-paused", auto_paused=True)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _strategic_task(db_session, project.id, "agent-vm-activate-paused")
    token = create_agent_token(
        str(task.id), "agent-vm-activate-paused", task_type="strategic_review"
    )

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "activate_direction",
            "data": {"direction_id": "d-activate-paused"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"

    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active"
    assert "auto_recovered_at" in d, "paused→active must stamp auto_recovered_at grace marker"
    assert "paused_at" not in d
    assert "auto_paused" not in d


@pytest.mark.asyncio
async def test_activate_direction_op_user_paused_allowed(
    client: AsyncClient, db_session
):
    """POST activate_direction on a user-paused (not auto-paused) direction is
    allowed — this is the sanctioned explicit reactivation path."""
    project = Project(
        name="Activate-UserPaused",
        user_id="activate-user3",
        autonomous_enabled=True,
        directions=[_paused_direction("d-user-paused", auto_paused=False)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _strategic_task(db_session, project.id, "agent-vm-activate-userpause")
    token = create_agent_token(
        str(task.id), "agent-vm-activate-userpause", task_type="strategic_review"
    )

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "activate_direction",
            "data": {"direction_id": "d-user-paused", "rationale": "operator approved resume"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    await db_session.refresh(project)
    assert project.directions[0]["status"] == "active"


@pytest.mark.asyncio
async def test_activate_direction_op_cap_reached(
    client: AsyncClient, db_session, monkeypatch
):
    """POST activate_direction when the active-direction cap is already reached
    returns a 409 skipped response and leaves the direction unchanged."""
    from app import shared_state

    monkeypatch.setitem(shared_state.runtime_config, "max_active_directions", 2)
    at_cap = [_active_direction(f"d-cap-{i}") for i in range(2)]
    project = Project(
        name="Activate-Cap",
        user_id="activate-user4",
        autonomous_enabled=True,
        directions=at_cap + [_staged_direction("d-staged-cap", staged_for_version=1)],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _strategic_task(db_session, project.id, "agent-vm-activate-cap")
    token = create_agent_token(
        str(task.id), "agent-vm-activate-cap", task_type="strategic_review"
    )

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "activate_direction",
            "data": {"direction_id": "d-staged-cap"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["status"] == "skipped"
    assert "cap reached" in body["reason"]

    await db_session.refresh(project)
    still_staged = next(d for d in project.directions if d["id"] == "d-staged-cap")
    assert still_staged["status"] == "staged", "direction must remain staged when cap is hit"


@pytest.mark.asyncio
async def test_activate_direction_op_wrong_status(client: AsyncClient, db_session):
    """POST activate_direction on an already-active direction returns 409 skipped;
    on a terminal (retired) direction also returns 409 skipped."""
    active_dir = _active_direction("d-already-active")
    retired_dir = {
        "id": "d-retired",
        "text": "Retired work",
        "status": "retired",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    project = Project(
        name="Activate-WrongStatus",
        user_id="activate-user5",
        autonomous_enabled=True,
        directions=[active_dir, retired_dir],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _strategic_task(db_session, project.id, "agent-vm-activate-wrong")
    token = create_agent_token(
        str(task.id), "agent-vm-activate-wrong", task_type="strategic_review"
    )

    # already active → 409 skipped
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "activate_direction",
            "data": {"direction_id": "d-already-active"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["status"] == "skipped"
    assert "already active" in resp.json()["reason"]

    # retired (terminal) → 409 skipped
    resp2 = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "activate_direction",
            "data": {"direction_id": "d-retired"},
        },
        headers={"x-agent-token": token},
    )
    assert resp2.status_code == 409, resp2.text
    assert resp2.json()["status"] == "skipped"
    assert "terminal" in resp2.json()["reason"]
