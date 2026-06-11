"""Tests for improvement_lane_ops: CAS transitions, checkpoint enforcement."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.improvement_lane_ops import (
    LaneCASError,
    LaneCheckpointError,
    cas_transition,
    checkpoint_lane,
    create_lane,
    mark_checkpoint_missing,
)
from app.models import (
    ImprovementLane,
    ImprovementLaneStatus,
    _LANE_MAX_PHASE_REQUEUES,
)
from app.payload_schemas import LaneCheckpointPayload, LaneCheckpointValidated


@pytest_asyncio.fixture()
async def db():
    """Isolated in-memory SQLite database for improvement_lane tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_project(session: AsyncSession) -> uuid.UUID:
    """Insert a minimal Project row and return its id."""
    from app.models import Project

    project_id = uuid.uuid4()
    session.add(
        Project(
            id=project_id,
            user_id="test-user",
            name="test-project",
            github_owner="owner",
            github_repo="repo",
            status="DEPLOYING",
        )
    )
    await session.commit()
    return project_id


# ---------------------------------------------------------------------------
# create_lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_lane_sets_proposed(db: AsyncSession):
    """create_lane should produce a lane in PROPOSED status."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id="d-abc123",
        title="Improve auth",
        goal="Reduce auth latency",
        branch_name="lane/abc12345",
        base_branch="main",
    )
    await db.commit()

    result = await db.execute(
        select(ImprovementLane).where(ImprovementLane.id == lane.id)
    )
    persisted = result.scalar_one()
    assert persisted.status == ImprovementLaneStatus.PROPOSED.value
    assert persisted.checkpoint_seq == 0
    assert persisted.checkpoints == []
    assert persisted.manifest == {}
    assert persisted.phase_requeues == 0
    assert persisted.current_task_id is None
    assert persisted.last_checkpoint_sha is None


# ---------------------------------------------------------------------------
# cas_transition — allowed transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cas_transition_proposed_to_active(db: AsyncSession):
    """PROPOSED -> ACTIVE should succeed and bind current_task_id."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t1",
        base_branch="main",
    )
    await db.commit()

    task_id = uuid.uuid4()
    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=task_id,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.ACTIVE.value
    assert updated.current_task_id == task_id
    assert updated.last_resumed_at is not None


@pytest.mark.asyncio
async def test_cas_transition_active_to_parked(db: AsyncSession):
    """ACTIVE -> PARKED should succeed and clear current_task_id."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t2",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.current_task_id = uuid.uuid4()
    await db.commit()

    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.ACTIVE,
        ImprovementLaneStatus.PARKED,
        clear_task=True,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.PARKED.value
    assert updated.current_task_id is None
    assert updated.parked_at is not None


@pytest.mark.asyncio
async def test_cas_transition_active_to_killed(db: AsyncSession):
    """ACTIVE -> KILLED should succeed and set retired_at."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t3",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    await db.commit()

    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.ACTIVE,
        ImprovementLaneStatus.KILLED,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.KILLED.value
    assert updated.retired_at is not None
    assert updated.current_task_id is None


@pytest.mark.asyncio
async def test_cas_transition_parked_to_active(db: AsyncSession):
    """PARKED -> ACTIVE should succeed (resume path)."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t4",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    await db.commit()

    task_id = uuid.uuid4()
    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.PARKED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=task_id,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.ACTIVE.value
    assert updated.current_task_id == task_id


# ---------------------------------------------------------------------------
# cas_transition — disallowed transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cas_transition_proposed_to_parked_rejected(db: AsyncSession):
    """PROPOSED -> PARKED is not in the transition graph; should raise."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t5",
        base_branch="main",
    )
    await db.commit()

    with pytest.raises(LaneCASError, match="not allowed"):
        await cas_transition(
            db,
            lane.id,
            ImprovementLaneStatus.PROPOSED,
            ImprovementLaneStatus.PARKED,
        )


@pytest.mark.asyncio
async def test_cas_transition_stale_status_raises(db: AsyncSession):
    """CAS with wrong expected status should raise LaneCASError."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t6",
        base_branch="main",
    )
    # Lane is PROPOSED, caller claims it is ACTIVE.
    await db.commit()

    with pytest.raises(LaneCASError, match="expected status ACTIVE"):
        await cas_transition(
            db,
            lane.id,
            ImprovementLaneStatus.ACTIVE,  # wrong expected
            ImprovementLaneStatus.PARKED,
        )


# ---------------------------------------------------------------------------
# checkpoint_lane — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_lane_appends_record_and_parks(db: AsyncSession):
    """checkpoint_lane should append a record and CAS the lane ACTIVE->PARKED."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/chk1",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.current_task_id = uuid.uuid4()
    await db.commit()

    task_id = uuid.uuid4()
    cp = LaneCheckpointPayload(
        sha="abc1234",
        summary="Implement login endpoint",
        validated=LaneCheckpointValidated(lint=True, tests="green"),
        wip_failing=[],
        manifest_patch={"progress": ["login endpoint added"]},
        tokens_in=1000,
        tokens_out=500,
        vm_cost_usd=0.05,
        warm_resume=False,
    )

    updated = await checkpoint_lane(db, lane.id, "create", task_id, cp)
    await db.commit()

    # Lane should now be PARKED.
    assert updated.status == ImprovementLaneStatus.PARKED.value
    assert updated.current_task_id is None

    # Checkpoint record appended.
    assert len(updated.checkpoints) == 1
    record = updated.checkpoints[0]
    assert record["seq"] == 1
    assert record["sha"] == "abc1234"
    assert record["phase"] == "create"
    assert record["task_id"] == str(task_id)
    assert record["validated"]["tests"] == "green"
    assert record["validated"]["lint"] is True

    # Checkpoint pointer fields updated.
    assert updated.last_checkpoint_sha == "abc1234"
    assert updated.last_checkpoint_phase == "create"
    assert updated.checkpoint_seq == 1

    # Accounting accumulated.
    assert updated.llm_input_tokens_total == 1000
    assert updated.llm_output_tokens_total == 500
    assert abs(updated.vm_cost_usd_total - 0.05) < 1e-6

    # Manifest patch merged.
    assert "login endpoint added" in updated.manifest.get("progress", [])


@pytest.mark.asyncio
async def test_checkpoint_lane_red_tests_still_checkpoints(db: AsyncSession):
    """A red test state may still checkpoint but must carry tests='red'."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/red1",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    await db.commit()

    cp = LaneCheckpointPayload(
        sha="def5678",
        summary="WIP: tests still failing",
        validated=LaneCheckpointValidated(lint=True, tests="red"),
        wip_failing=["testing/test_auth.py::test_login"],
    )

    updated = await checkpoint_lane(db, lane.id, "fix", uuid.uuid4(), cp)
    await db.commit()

    assert updated.status == ImprovementLaneStatus.PARKED.value
    record = updated.checkpoints[0]
    assert record["validated"]["tests"] == "red"
    assert "testing/test_auth.py::test_login" in record["wip_failing"]


@pytest.mark.asyncio
async def test_checkpoint_lane_rolling_window(db: AsyncSession):
    """Checkpoints list is bounded to _LANE_CHECKPOINT_LIMIT entries."""
    from app.models import _LANE_CHECKPOINT_LIMIT

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/roll1",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    await db.commit()

    # Seed the checkpoints list with _LANE_CHECKPOINT_LIMIT existing entries.
    lane.checkpoints = [{"seq": i, "sha": f"sha{i:04d}"} for i in range(_LANE_CHECKPOINT_LIMIT)]
    lane.checkpoint_seq = _LANE_CHECKPOINT_LIMIT
    await db.commit()

    cp = LaneCheckpointPayload(
        sha="newsha99",
        summary="overflow checkpoint",
    )
    updated = await checkpoint_lane(db, lane.id, "create", uuid.uuid4(), cp)
    await db.commit()

    assert len(updated.checkpoints) == _LANE_CHECKPOINT_LIMIT
    # The oldest entry was evicted; newest is the one we just appended.
    assert updated.checkpoints[-1]["sha"] == "newsha99"
    assert updated.checkpoints[0]["sha"] == "sha0001"


@pytest.mark.asyncio
async def test_checkpoint_lane_not_active_raises(db: AsyncSession):
    """checkpoint_lane on a PARKED lane should raise LaneCheckpointError."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/err1",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    await db.commit()

    cp = LaneCheckpointPayload(sha="abc1234", summary="bad checkpoint attempt")
    with pytest.raises(LaneCheckpointError, match="must be ACTIVE"):
        await checkpoint_lane(db, lane.id, "create", uuid.uuid4(), cp)


# ---------------------------------------------------------------------------
# mark_checkpoint_missing — requeue budgeting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_checkpoint_missing_increments_requeues(db: AsyncSession):
    """mark_checkpoint_missing should increment phase_requeues and return should_requeue=True."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/miss1",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    await db.commit()

    updated, should_requeue = await mark_checkpoint_missing(db, lane.id)
    await db.commit()

    assert should_requeue is True
    assert updated.phase_requeues == 1


@pytest.mark.asyncio
async def test_mark_checkpoint_missing_exhausted_budget(db: AsyncSession):
    """After _LANE_MAX_PHASE_REQUEUES exhaustion, should_requeue should be False."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/miss2",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.phase_requeues = _LANE_MAX_PHASE_REQUEUES  # already at budget
    await db.commit()

    updated, should_requeue = await mark_checkpoint_missing(db, lane.id)
    await db.commit()

    assert should_requeue is False
    assert updated.phase_requeues == _LANE_MAX_PHASE_REQUEUES + 1


# ---------------------------------------------------------------------------
# manifest bounding
# ---------------------------------------------------------------------------


def test_merge_manifest_patch_bounded():
    """_merge_manifest_patch trims the result to stay under _LANE_MANIFEST_MAX_BYTES."""
    import json

    from app.improvement_lane_ops import _merge_manifest_patch
    from app.models import _LANE_MANIFEST_MAX_BYTES

    # Existing manifest that's already near the limit via a long phase_log.
    existing = {"goal": "do X", "phase_log": [{"info": "y" * 100} for _ in range(40)]}
    # Small patch that would push it over if unbounded.
    patch = {"phase_log": [{"info": "z" * 100} for _ in range(10)]}

    merged = _merge_manifest_patch(existing, patch)
    size = len(json.dumps(merged, default=str).encode())
    assert size <= _LANE_MANIFEST_MAX_BYTES, f"merged manifest exceeds 8KB: {size} bytes"


# ---------------------------------------------------------------------------
# LaneCheckpointPayload validation
# ---------------------------------------------------------------------------


def test_lane_checkpoint_payload_valid():
    """Valid payload should parse without error."""
    cp = LaneCheckpointPayload(sha="abc1234", summary="ok")
    assert cp.validated.tests == "green"
    assert cp.validated.lint is True
    assert cp.wip_failing == []


def test_lane_checkpoint_payload_invalid_test_state():
    """tests must be 'green', 'red', or 'skipped'."""
    import pytest

    with pytest.raises(Exception, match="pattern"):
        LaneCheckpointValidated(lint=True, tests="yellow")


def test_lane_checkpoint_payload_manifest_patch_too_large():
    """manifest_patch > 8KB should raise a validation error."""
    big_data = {"key": "x" * 9000}
    with pytest.raises(Exception, match="8KB"):
        LaneCheckpointPayload(sha="abc1234", summary="overflow", manifest_patch=big_data)
