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

from datetime import datetime, timedelta

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
from app.models import Direction, Project, Task, TaskStatus
from app.reaper_sweeps import sweep_activate_staged_directions
from app.strategic_spawning import _build_staged_directions_summary


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
                    datetime.utcnow() - timedelta(hours=10)
                ).isoformat(),
            )
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active = await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240, busy_direction_ids=set(), now=datetime.utcnow()
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
    # Version 2 is OPEN — so version 1 has already promoted.
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()

    total = await sweep_activate_staged_directions(db_session)
    assert total == 1

    await db_session.refresh(project)
    by_id = {d["id"]: d["status"] for d in project.directions}
    assert by_id["d-ready"] == "active"
    assert by_id["d-hold"] == "staged"

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
    staged_for_version}; active/paused/proposed are excluded."""
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


def test_build_staged_directions_summary_empty_when_none_staged():
    """No staged directions → empty list so the payload omits the field."""
    project = Project(
        name="No-Staged",
        user_id="stage-user",
        directions=[_active_direction("d-a"), _active_direction("d-b")],
    )
    assert _build_staged_directions_summary(project) == []
