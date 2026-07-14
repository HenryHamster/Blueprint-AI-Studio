"""Tests for improvement_lane_ops: CAS transitions, checkpoint enforcement."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from testing.factories import make_project, make_task

from app.time_utils import _utcnow
from app.improvement_lane_ops import (
    LaneCASError,
    LaneCheckpointError,
    LaneOwnerMismatchError,
    cas_transition,
    checkpoint_lane,
    create_lane,
    mark_checkpoint_missing,
)
from app.models import (
    ImprovementLane,
    ImprovementLaneStatus,
    PoolVm,
    PoolVmState,
    _LANE_MAX_PHASE_REQUEUES,
)
from app.payload_schemas import LaneCheckpointPayload, LaneCheckpointValidated
from app.review_fix_lane_pilot import (
    LANE_CHAIN_ID_KEY,
    activate_lane_for_fix,
    ensure_chain_lane,
    get_chain_lane,
    kill_project_lanes,
    retire_chain_lane,
)


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
async def test_cas_transition_proposed_to_killed(db: AsyncSession):
    """PROPOSED -> KILLED should succeed (stale/failed-activation aging) and
    stamp retired_at while clearing any current_task_id binding."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t3k",
        base_branch="main",
    )
    # A PROPOSED lane that briefly bound a task during a failed activation.
    lane.current_task_id = uuid.uuid4()
    await db.commit()

    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.KILLED,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.KILLED.value
    assert updated.retired_at is not None
    assert updated.current_task_id is None


@pytest.mark.asyncio
async def test_cas_transition_proposed_to_retired(db: AsyncSession):
    """PROPOSED -> RETIRED should succeed (chain merged/closed before the lane
    ever activated)."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/t3r",
        base_branch="main",
    )
    await db.commit()

    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.RETIRED,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.RETIRED.value
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
    task_id = uuid.uuid4()
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.current_task_id = task_id
    await db.commit()

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
    task_id = uuid.uuid4()
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.current_task_id = task_id
    await db.commit()

    cp = LaneCheckpointPayload(
        sha="def5678",
        summary="WIP: tests still failing",
        validated=LaneCheckpointValidated(lint=True, tests="red"),
        wip_failing=["testing/test_auth.py::test_login"],
    )

    updated = await checkpoint_lane(db, lane.id, "fix", task_id, cp)
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
    task_id = uuid.uuid4()
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.current_task_id = task_id
    await db.commit()

    # Seed the checkpoints list with _LANE_CHECKPOINT_LIMIT existing entries.
    lane.checkpoints = [
        {"seq": i, "sha": f"sha{i:04d}"} for i in range(_LANE_CHECKPOINT_LIMIT)
    ]
    lane.checkpoint_seq = _LANE_CHECKPOINT_LIMIT
    await db.commit()

    cp = LaneCheckpointPayload(
        sha="newsha99",
        summary="overflow checkpoint",
    )
    updated = await checkpoint_lane(db, lane.id, "create", task_id, cp)
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


@pytest.mark.asyncio
async def test_checkpoint_lane_rejects_non_owner_task(db: AsyncSession):
    """checkpoint_lane raises LaneOwnerMismatchError when task_id != current_task_id.

    The lane's status, current_task_id, and last_checkpoint_sha must remain
    unchanged after the rejection — the new owner is unaffected.
    """
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/owner1",
        base_branch="main",
    )
    owner_id = uuid.uuid4()
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.current_task_id = owner_id
    lane.last_checkpoint_sha = "original_sha"
    await db.commit()
    # Snapshot the id BEFORE the rollback below: rollback expires every loaded
    # instance, and a sync read of an expired attribute on an async session
    # raises MissingGreenlet under real ORM expiry.
    lane_id = lane.id

    stale_task_id = uuid.uuid4()
    cp = LaneCheckpointPayload(sha="stale_sha", summary="stale checkpoint attempt")
    with pytest.raises(LaneOwnerMismatchError, match=str(owner_id)):
        await checkpoint_lane(db, lane_id, "create", stale_task_id, cp)

    # Verify the lane is untouched — the DB session may have partial state,
    # so re-fetch to confirm.
    await db.rollback()
    refreshed = await db.get(ImprovementLane, lane_id)
    assert refreshed.status == ImprovementLaneStatus.ACTIVE.value
    assert refreshed.current_task_id == owner_id
    assert refreshed.last_checkpoint_sha == "original_sha"


@pytest.mark.asyncio
async def test_checkpoint_lane_rejects_none_owner_active_lane(db: AsyncSession):
    """An ACTIVE lane with current_task_id=None rejects any foreign checkpoint.

    Before the fix, the guard read ``if current_task_id is not None and ...``
    which short-circuits to False when current_task_id is None — silently
    admitting any caller.  After the fix the guard is unconditional so None
    never matches a real task_id and raises LaneOwnerMismatchError.
    """
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/none-owner1",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    # Deliberately leave current_task_id as None (no owner stamped yet).
    await db.commit()
    # Snapshot the id BEFORE the rollback below (rollback expires instances;
    # an expired sync read raises MissingGreenlet under real ORM expiry).
    lane_id = lane.id

    foreign_task_id = uuid.uuid4()
    cp = LaneCheckpointPayload(sha="foreign_sha", summary="should be rejected")
    with pytest.raises(LaneOwnerMismatchError):
        await checkpoint_lane(db, lane_id, "create", foreign_task_id, cp)

    # Lane must be untouched after the rejection.
    await db.rollback()
    refreshed = await db.get(ImprovementLane, lane_id)
    assert refreshed.status == ImprovementLaneStatus.ACTIVE.value
    assert refreshed.current_task_id is None
    assert refreshed.last_checkpoint_sha is None


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


@pytest.mark.asyncio
async def test_mark_checkpoint_missing_emits_pre_exhaustion_warning(
    db: AsyncSession, caplog
):
    """When phase_requeues reaches _LANE_MAX_PHASE_REQUEUES - 1, emit WARNING alert."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/miss-pre-exhaust",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    # Start with phase_requeues at _LANE_MAX_PHASE_REQUEUES - 2, so after increment
    # it will be at _LANE_MAX_PHASE_REQUEUES - 1 (two out of three used)
    lane.phase_requeues = _LANE_MAX_PHASE_REQUEUES - 2
    await db.commit()

    with caplog.at_level("WARNING"):
        updated, should_requeue = await mark_checkpoint_missing(
            db, lane.id, reason="test_reason"
        )
    await db.commit()

    assert should_requeue is True
    assert updated.phase_requeues == _LANE_MAX_PHASE_REQUEUES - 1
    # Verify that the pre-exhaustion warning was emitted
    assert "approaching phase_requeues exhaustion" in caplog.text
    assert "one more miss will trigger kill" in caplog.text
    assert str(lane.id) in caplog.text


@pytest.mark.asyncio
async def test_mark_checkpoint_missing_no_warning_when_under_limit(
    db: AsyncSession, caplog
):
    """When phase_requeues is below _LANE_MAX_PHASE_REQUEUES - 1, no pre-exhaustion warning."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/miss-under-limit",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    # Start with phase_requeues = 0, so after increment it will be 1
    # (well under the _LANE_MAX_PHASE_REQUEUES - 1 threshold of 2)
    lane.phase_requeues = 0
    await db.commit()

    with caplog.at_level("WARNING"):
        updated, should_requeue = await mark_checkpoint_missing(db, lane.id)
    await db.commit()

    assert should_requeue is True
    assert updated.phase_requeues == 1
    # Verify that the pre-exhaustion warning was NOT emitted
    assert "approaching phase_requeues exhaustion" not in caplog.text


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
    assert size <= _LANE_MANIFEST_MAX_BYTES, (
        f"merged manifest exceeds 8KB: {size} bytes"
    )


def test_merge_manifest_patch_eviction_priority_phase_log_before_critical_files():
    """When phase_log and critical_files are the same length, phase_log is evicted first."""
    import json

    from app.improvement_lane_ops import _merge_manifest_patch
    from app.models import _LANE_MANIFEST_MAX_BYTES

    # Build a manifest over the byte limit with equal-length phase_log and critical_files.
    item = "x" * 200
    existing = {
        "phase_log": [item] * 25,
        "critical_files": [item] * 25,
    }
    # Confirm it's already over budget so trimming fires.
    assert len(json.dumps(existing, default=str).encode()) > _LANE_MANIFEST_MAX_BYTES

    # A no-op patch — just trigger the trim of the existing manifest.
    merged = _merge_manifest_patch(existing, {})

    phase_log_len = len(merged.get("phase_log", []))
    critical_files_len = len(merged.get("critical_files", []))

    assert phase_log_len < critical_files_len, (
        f"phase_log ({phase_log_len}) should be shorter than "
        f"critical_files ({critical_files_len}) after eviction"
    )


def test_merge_manifest_patch_bounded_with_all_append_only_lists():
    """Long critical_files and remaining lists are trimmed like other append-only fields."""
    import json

    from app.improvement_lane_ops import _merge_manifest_patch
    from app.models import _LANE_MANIFEST_MAX_BYTES

    existing = {
        "phase_log": ["p" * 220 for _ in range(18)],
        "progress": ["progress " + "x" * 220 for _ in range(18)],
        "critical_files": [f"critical/{i}/" + "c" * 180 for i in range(18)],
        "remaining": [f"[MAJOR] issue {i}: " + "r" * 180 for i in range(18)],
        "failing_tests": ["test_" + "t" * 220 for _ in range(18)],
        "risks": ["risk " + "k" * 220 for _ in range(18)],
    }
    patch = {
        "critical_files": [f"critical/new/{i}/" + "n" * 180 for i in range(6)],
        "remaining": [f"[MAJOR] new issue {i}: " + "m" * 180 for i in range(6)],
    }

    merged = _merge_manifest_patch(existing, patch)
    size = len(json.dumps(merged, default=str).encode())
    assert size <= _LANE_MANIFEST_MAX_BYTES, (
        f"merged manifest exceeds 8KB: {size} bytes"
    )


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
        LaneCheckpointPayload(
            sha="abc1234", summary="overflow", manifest_patch=big_data
        )


# ---------------------------------------------------------------------------
# build_lane_manifest_patch / build_fix_resume_block — bounded derivation
# ---------------------------------------------------------------------------


class _FakeTask:
    """Minimal stand-in for a Task exposing .context and .first_payload."""

    def __init__(self, context: dict, payloads: list[dict]):
        self.context = context
        self._payloads = payloads

    @property
    def first_payload(self) -> dict:
        return self._payloads[0] if self._payloads else {}


def test_build_lane_manifest_patch_uses_known_list_fields():
    """Patch is built from durable phase state using only the merge-bounded keys."""
    from app.review_fix_lane_pilot import build_lane_manifest_patch

    task = _FakeTask(
        context={
            "_pr_changed_files": ["backend/app/foo.py", "agent/src/bar.ts"],
            "investigated_files": ["backend/app/foo.py", "backend/app/baz.py"],
            "_test_failure_summary": "test_foo failed: AssertionError",
            "review_loop": {
                "iteration": 2,
                "review_history": [
                    {
                        "issues": [
                            {
                                "severity": "major",
                                "description": "null case",
                                "file": "a.ts",
                            }
                        ]
                    },
                ],
                "fix_agent_responses": [
                    {"response_type": "disputed", "claim": "flag is intentional"},
                ],
            },
        },
        payloads=[{"type": "fix", "pr_number": 42}],
    )
    patch = build_lane_manifest_patch(task, summary="addressed review")

    # Only the bounded list-field keys the merge understands appear.
    assert set(patch).issubset(
        {
            "progress",
            "remaining",
            "critical_files",
            "failing_tests",
            "risks",
            "phase_log",
        }
    )
    assert patch["phase_log"] == ["fix PR#42 iter2: addressed review"]
    # changed + investigated files, deduped, order-preserving.
    assert patch["critical_files"] == [
        "backend/app/foo.py",
        "agent/src/bar.ts",
        "backend/app/baz.py",
    ]
    assert patch["failing_tests"] == ["test_foo failed: AssertionError"]
    assert patch["remaining"] == ["[MAJOR] [a.ts] null case"]
    assert patch["risks"] == ["disputed: flag is intentional"]


def test_build_lane_manifest_patch_minimal_when_no_phase_state():
    """A bare phase with no context still yields a minimal phase_log patch."""
    from app.review_fix_lane_pilot import build_lane_manifest_patch

    task = _FakeTask(context={}, payloads=[{"type": "create"}])
    patch = build_lane_manifest_patch(task, summary="built it")
    assert patch == {"phase_log": ["create: built it"]}


def test_build_lane_manifest_patch_is_size_bounded():
    """A pathological set of long strings is trimmed under the patch byte bound."""
    import json

    from app.review_fix_lane_pilot import (
        _MANIFEST_PATCH_MAX_BYTES,
        build_lane_manifest_patch,
    )

    task = _FakeTask(
        context={
            "_pr_changed_files": ["x" * 500 for _ in range(40)],
            "review_loop": {
                "review_history": [
                    {
                        "issues": [
                            {"severity": "major", "description": "y" * 500}
                            for _ in range(40)
                        ]
                    },
                ],
                "fix_agent_responses": [{"claim": "z" * 500} for _ in range(40)],
            },
        },
        payloads=[{"type": "fix", "pr_number": 1}],
    )
    patch = build_lane_manifest_patch(task, summary="s")
    size = len(json.dumps(patch, default=str).encode())
    assert size <= _MANIFEST_PATCH_MAX_BYTES, f"patch exceeds bound: {size}"
    # And it stays under the LaneCheckpointPayload 8KB validator.
    LaneCheckpointPayload(sha="abc1234", summary="s", manifest_patch=patch)


def test_build_lane_manifest_patch_accumulates_under_8kb_across_phases():
    """Repeated patches merged into a manifest stay under the 8KB lane contract."""
    import json

    from app.improvement_lane_ops import _merge_manifest_patch
    from app.models import _LANE_MANIFEST_MAX_BYTES
    from app.review_fix_lane_pilot import build_lane_manifest_patch

    manifest: dict = {}
    for i in range(40):
        task = _FakeTask(
            context={
                "_pr_changed_files": [
                    f"deeply/nested/path/{i}/file_{j}_" + "x" * 160 + ".py"
                    for j in range(6)
                ],
                "review_loop": {
                    "iteration": i,
                    "review_history": [
                        {
                            "issues": [
                                {
                                    "severity": "major",
                                    "file": f"deeply/nested/path/{i}/issue_{j}.py",
                                    "description": f"issue {i}-{j} " + "y" * 180,
                                }
                                for j in range(6)
                            ]
                        },
                    ],
                },
            },
            payloads=[{"type": "fix", "pr_number": 100 + i}],
        )
        patch = build_lane_manifest_patch(task, summary=f"phase {i} " + "d" * 100)
        manifest = _merge_manifest_patch(manifest, patch)

    size = len(json.dumps(manifest, default=str).encode())
    assert size <= _LANE_MANIFEST_MAX_BYTES, f"accumulated manifest exceeds 8KB: {size}"


def test_build_fix_resume_block_from_manifest_and_checkpoint():
    """Resume block surfaces manifest list fields + checkpoint pointers."""
    from types import SimpleNamespace

    from app.review_fix_lane_pilot import build_fix_resume_block

    lane = SimpleNamespace(
        manifest={
            "phase_log": ["create PR#1: built", "fix PR#1: fixed"],
            "critical_files": ["a.py", "b.ts"],
            "remaining": ["[MAJOR] open issue"],
            "risks": ["disputed: x"],
        },
        last_checkpoint_sha="deadbeef1234",
        last_checkpoint_phase="fix",
        checkpoint_seq=2,
    )
    block = build_fix_resume_block(lane)
    assert block is not None
    assert block["checkpoint_seq"] == 2
    assert block["last_checkpoint_sha"] == "deadbeef1234"
    assert block["last_phase"] == "fix"
    assert block["phase_log"] == ["create PR#1: built", "fix PR#1: fixed"]
    assert block["critical_files"] == ["a.py", "b.ts"]
    assert block["remaining"] == ["[MAJOR] open issue"]


def test_build_fix_resume_block_none_when_empty():
    """No manifest content and no checkpoint sha → no block to attach."""
    from types import SimpleNamespace

    from app.review_fix_lane_pilot import build_fix_resume_block

    assert build_fix_resume_block(None) is None
    empty = SimpleNamespace(
        manifest={},
        last_checkpoint_sha=None,
        last_checkpoint_phase=None,
        checkpoint_seq=0,
    )
    assert build_fix_resume_block(empty) is None


def test_build_fix_resume_block_is_size_bounded():
    """A large manifest yields a resume block under the resume byte bound."""
    import json
    from types import SimpleNamespace

    from app.review_fix_lane_pilot import (
        _RESUME_BLOCK_MAX_BYTES,
        build_fix_resume_block,
    )

    lane = SimpleNamespace(
        manifest={
            "phase_log": ["p" * 300 for _ in range(20)],
            "critical_files": ["f" * 300 for _ in range(20)],
            "risks": ["r" * 300 for _ in range(20)],
        },
        last_checkpoint_sha="abc1234",
        last_checkpoint_phase="fix",
        checkpoint_seq=9,
    )
    block = build_fix_resume_block(lane)
    assert block is not None
    size = len(json.dumps(block, default=str).encode())
    assert size <= _RESUME_BLOCK_MAX_BYTES, f"resume block exceeds bound: {size}"


# ---------------------------------------------------------------------------
# retire_chain_lane — terminal lifecycle on PR merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_terminal_retires_lane_and_releases_reservation(db: AsyncSession):
    """retire_chain_lane CASes ACTIVE -> RETIRED and clears any PoolVm reserved for
    the lane, so the member rejoins the general pool immediately rather than waiting
    for the TTL expiry sweep."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id="d-retire1",
        title="Retire on merge",
        goal="PR merged goal",
        branch_name="lane/retire1",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    # A warm-pool member reserved for this lane.
    member = PoolVm(
        vm_instance_id="pool-vm-retire-test-1",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
    )
    db.add(member)
    await db.commit()

    ctx = {LANE_CHAIN_ID_KEY: str(lane.id)}
    did_retire = await retire_chain_lane(db, ctx)
    await db.commit()

    assert did_retire is True
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value
    assert refreshed.retired_at is not None
    assert refreshed.current_task_id is None
    # Reservation must be released so the member is immediately available.
    await db.refresh(member)
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None


@pytest.mark.asyncio
async def test_retire_chain_lane_records_force_expired_and_clears_hard_anchor(
    db: AsyncSession,
):
    """Retiring a chain force-expires the lane's reservation: the hard ceiling
    anchor is cleared AND the force-expired expiry-cause counter is incremented so
    a low warm_resume_fraction stays diagnosable."""
    from app.time_utils import _utcnow
    from datetime import timedelta

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id="d-retire-cause",
        title="Retire records cause",
        goal="goal",
        branch_name="lane/retire-cause",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    member = PoolVm(
        vm_instance_id="pool-vm-retire-cause",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
        reservation_expires_at=_utcnow() + timedelta(seconds=300),
        reservation_hard_expires_at=_utcnow() + timedelta(seconds=3600),
    )
    db.add(member)
    await db.commit()

    ctx = {LANE_CHAIN_ID_KEY: str(lane.id)}
    assert await retire_chain_lane(db, ctx) is True
    await db.commit()

    await db.refresh(member)
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None
    assert member.reservation_hard_expires_at is None, (
        "the hard ceiling anchor must be cleared on force-expiry"
    )

    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.reservation_force_expired_count == 1
    assert refreshed.reservation_claimed_by_fix_count == 0
    assert refreshed.reservation_expired_before_fix_count == 0


@pytest.mark.asyncio
async def test_retire_chain_lane_parked_becomes_retired(db: AsyncSession):
    """retire_chain_lane CASes PARKED -> RETIRED as well."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/retire2",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    await db.commit()

    ctx = {LANE_CHAIN_ID_KEY: str(lane.id)}
    did_retire = await retire_chain_lane(db, ctx)
    await db.commit()

    assert did_retire is True
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value


@pytest.mark.asyncio
async def test_retire_chain_lane_noop_if_terminal(db: AsyncSession):
    """retire_chain_lane is a no-op when the lane is already in a terminal state."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/retire3",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.KILLED.value
    await db.commit()

    ctx = {LANE_CHAIN_ID_KEY: str(lane.id)}
    did_retire = await retire_chain_lane(db, ctx)
    await db.commit()

    assert did_retire is False
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.KILLED.value


@pytest.mark.asyncio
async def test_retire_chain_lane_noop_without_chain_id(db: AsyncSession):
    """retire_chain_lane returns False and is a no-op when ctx lacks LANE_CHAIN_ID_KEY."""
    did_retire = await retire_chain_lane(db, {})
    assert did_retire is False


@pytest.mark.asyncio
async def test_retire_chain_lane_shadow_parked_no_reservation(db: AsyncSession):
    """Shadow-mode (armless) lanes have no warm-pool reservation but accumulate
    PARKED rows.  retire_chain_lane must still CAS PARKED -> RETIRED and return
    True even when no PoolVm is reserved for the lane."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Shadow lane",
        goal="Shadow goal",
        branch_name="lane/shadow-retire",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    # No PoolVm reservation — this is the shadow (armless) path.
    await db.commit()

    ctx = {LANE_CHAIN_ID_KEY: str(lane.id)}
    did_retire = await retire_chain_lane(db, ctx)
    await db.commit()

    assert did_retire is True, (
        "retire_chain_lane must return True for PARKED shadow lane"
    )
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value, (
        f"Shadow PARKED lane must become RETIRED at chain end, got {refreshed.status}"
    )


# ---------------------------------------------------------------------------
# kill_project_lanes — bulk KILL on pilot termination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_project_lanes_transitions_active_and_parked(db: AsyncSession):
    """kill_project_lanes CASes both ACTIVE and PARKED lanes to KILLED."""
    project_id = await _make_project(db)

    lane_active = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Active lane",
        goal="G",
        branch_name="lane/kill1a",
        base_branch="main",
    )
    lane_active.status = ImprovementLaneStatus.ACTIVE.value

    lane_parked = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Parked lane",
        goal="G",
        branch_name="lane/kill1p",
        base_branch="main",
    )
    lane_parked.status = ImprovementLaneStatus.PARKED.value
    await db.commit()

    count = await kill_project_lanes(db, project_id)
    await db.commit()

    assert count == 2
    for lane_id in (lane_active.id, lane_parked.id):
        refreshed = await db.get(ImprovementLane, lane_id)
        assert refreshed.status == ImprovementLaneStatus.KILLED.value
        assert refreshed.retired_at is not None


@pytest.mark.asyncio
async def test_kill_project_lanes_skips_already_terminal(db: AsyncSession):
    """kill_project_lanes does not re-transition lanes already in RETIRED or KILLED."""
    project_id = await _make_project(db)

    lane_retired = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Already retired",
        goal="G",
        branch_name="lane/kill2r",
        base_branch="main",
    )
    lane_retired.status = ImprovementLaneStatus.RETIRED.value

    lane_killed = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Already killed",
        goal="G",
        branch_name="lane/kill2k",
        base_branch="main",
    )
    lane_killed.status = ImprovementLaneStatus.KILLED.value
    await db.commit()

    count = await kill_project_lanes(db, project_id)
    await db.commit()

    assert count == 0
    # Statuses unchanged.
    assert (
        await db.get(ImprovementLane, lane_retired.id)
    ).status == ImprovementLaneStatus.RETIRED.value
    assert (
        await db.get(ImprovementLane, lane_killed.id)
    ).status == ImprovementLaneStatus.KILLED.value


# ---------------------------------------------------------------------------
# sweep_exhausted_lane_budgets — reaper kills over-budget ACTIVE lanes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_exhausted_lane_budgets_kills_over_budget(db: AsyncSession):
    """Over-budget ACTIVE lane is killed; at-budget and under-budget lanes are preserved."""
    from app.warm_pool_maintainer import sweep_exhausted_lane_budgets

    project_id = await _make_project(db)

    lane_over = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Over-budget lane",
        goal="G",
        branch_name="lane/sweep1o",
        base_branch="main",
    )
    lane_over.status = ImprovementLaneStatus.ACTIVE.value
    lane_over.phase_requeues = _LANE_MAX_PHASE_REQUEUES + 1  # strictly over budget

    lane_at = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="At-budget lane",
        goal="G",
        branch_name="lane/sweep1a",
        base_branch="main",
    )
    lane_at.status = ImprovementLaneStatus.ACTIVE.value
    lane_at.phase_requeues = _LANE_MAX_PHASE_REQUEUES  # exactly at limit, not over

    lane_under = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Under-budget lane",
        goal="G",
        branch_name="lane/sweep1u",
        base_branch="main",
    )
    lane_under.status = ImprovementLaneStatus.ACTIVE.value
    lane_under.phase_requeues = 0

    await db.commit()

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_exhausted_lane_budgets(db)
    await db.commit()

    assert count == 1
    assert (
        await db.get(ImprovementLane, lane_over.id)
    ).status == ImprovementLaneStatus.KILLED.value
    # at-budget and under-budget lanes must not be touched
    assert (
        await db.get(ImprovementLane, lane_at.id)
    ).status == ImprovementLaneStatus.ACTIVE.value
    assert (
        await db.get(ImprovementLane, lane_under.id)
    ).status == ImprovementLaneStatus.ACTIVE.value


# ---------------------------------------------------------------------------
# ensure_chain_lane — activation failure must not bind the chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_lane_activation_does_not_bind_chain(db: AsyncSession):
    """ensure_chain_lane must not write LANE_CHAIN_ID_KEY when CAS PROPOSED->ACTIVE
    fails.  The function must return None so the chain falls through to classic relay.
    No PROPOSED lane must remain referenced by the chain context."""
    from app.improvement_lane_ops import LaneCASError

    project_id = await _make_project(db)
    ctx: dict = {}

    async def _fail_on_activate(session, lane_id, expected, next_status, **kw):
        if next_status == ImprovementLaneStatus.ACTIVE:
            raise LaneCASError("simulated CAS race on activation")
        # Cleanup call (PROPOSED->RETIRED) — best-effort, may be suppressed.

    with patch(
        "app.improvement_lane_ops.cas_transition", side_effect=_fail_on_activate
    ):
        result = await ensure_chain_lane(
            db,
            project_id=project_id,
            ctx=ctx,
            title="Test lane",
            branch_name="lane/test-activation-fail",
            base_branch="main",
            direction_id=None,
            current_task_id=None,
        )

    # Return must be None — activation failed so no lane is usable.
    assert result is None

    # Chain context must not carry a lane binding — no PROPOSED lane referenced.
    assert LANE_CHAIN_ID_KEY not in ctx

    # get_chain_lane must also return None for the empty ctx.
    lane_via_get = await get_chain_lane(db, ctx)
    assert lane_via_get is None


@pytest.mark.asyncio
async def test_get_chain_lane_ignores_proposed_lane(db: AsyncSession):
    """get_chain_lane returns None for a PROPOSED lane — only ACTIVE/PARKED are valid."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/proposed-guard",
        base_branch="main",
    )
    await db.commit()
    # Lane is in PROPOSED status (the default from create_lane).
    assert lane.status == ImprovementLaneStatus.PROPOSED.value

    ctx = {LANE_CHAIN_ID_KEY: str(lane.id)}
    result = await get_chain_lane(db, ctx)
    assert result is None, "get_chain_lane must not return a PROPOSED lane"


@pytest.mark.asyncio
async def test_sweep_exhausted_lane_budgets_inert_when_disabled(db: AsyncSession):
    """sweep_exhausted_lane_budgets returns 0 and changes nothing when the flag is off."""
    from app.warm_pool_maintainer import sweep_exhausted_lane_budgets

    project_id = await _make_project(db)

    lane_over = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="Over-budget but flag off",
        goal="G",
        branch_name="lane/sweep2o",
        base_branch="main",
    )
    lane_over.status = ImprovementLaneStatus.ACTIVE.value
    lane_over.phase_requeues = _LANE_MAX_PHASE_REQUEUES + 1
    await db.commit()

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=False
    ):
        count = await sweep_exhausted_lane_budgets(db)

    assert count == 0
    # Lane must remain ACTIVE — the sweep was entirely inert.
    assert (
        await db.get(ImprovementLane, lane_over.id)
    ).status == ImprovementLaneStatus.ACTIVE.value


# ── confirm_lane_warm_resume unit tests ──────────────────────────────────────


def test_confirm_lane_warm_resume_with_checkpoint():
    """has_checkpoint=True stamps both receipt keys as True."""
    from app.capability_trial_verdict import (
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    )
    from app.review_fix_lane_pilot import confirm_lane_warm_resume

    ctx: dict = {}
    confirm_lane_warm_resume(ctx, has_checkpoint=True)

    assert ctx[LANE_PILOT_WARM_RESUME_RECEIPT_KEY] is True
    assert ctx[LANE_PILOT_CHECKPOINT_RECEIPT_KEY] is True


def test_confirm_lane_warm_resume_without_checkpoint():
    """has_checkpoint=False stamps warm key True but checkpoint key False."""
    from app.capability_trial_verdict import (
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    )
    from app.review_fix_lane_pilot import confirm_lane_warm_resume

    ctx: dict = {}
    confirm_lane_warm_resume(ctx, has_checkpoint=False)

    assert ctx[LANE_PILOT_WARM_RESUME_RECEIPT_KEY] is True
    assert ctx[LANE_PILOT_CHECKPOINT_RECEIPT_KEY] is False


def test_confirm_lane_warm_resume_empty_dict():
    """Calling on an empty dict produces exactly the two receipt keys."""
    from app.capability_trial_verdict import (
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    )
    from app.review_fix_lane_pilot import confirm_lane_warm_resume

    ctx: dict = {}
    confirm_lane_warm_resume(ctx, has_checkpoint=True)

    assert set(ctx.keys()) == {
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
    }


def test_confirm_lane_warm_resume_existing_keys_overwritten():
    """Stale values in ctx are overwritten; no duplication or extra keys introduced."""
    from app.capability_trial_verdict import (
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    )
    from app.review_fix_lane_pilot import confirm_lane_warm_resume

    ctx: dict = {
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY: False,
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY: False,
        "other_key": "preserved",
    }
    confirm_lane_warm_resume(ctx, has_checkpoint=True)

    assert ctx[LANE_PILOT_WARM_RESUME_RECEIPT_KEY] is True
    assert ctx[LANE_PILOT_CHECKPOINT_RECEIPT_KEY] is True
    assert ctx["other_key"] == "preserved"


# ---------------------------------------------------------------------------
# sweep_stranded_lanes — Class 3: PARKED lane retirement via terminal spawn_reason
# ---------------------------------------------------------------------------


async def _make_parked_lane(session, project_id, *, parked_ago_seconds: int = 7200):
    """Create a PARKED lane with parked_at set to `parked_ago_seconds` ago."""
    from datetime import timedelta

    lane = await create_lane(
        session,
        project_id=project_id,
        direction_id=None,
        title="Parked test lane",
        goal="G",
        branch_name=f"lane/parked-{uuid.uuid4().hex[:8]}",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    lane.parked_at = _utcnow() - timedelta(seconds=parked_ago_seconds)
    await session.commit()
    return lane


async def _make_terminal_task(session, project_id, lane_id, spawn_reason: str):
    """Create a terminal Task with LANE_CHAIN_ID_KEY and spawn_reason set."""
    from app.models import Task, TaskStatus
    from app.review_pipeline import SPAWN_REASON_KEY

    task = Task(
        project_id=project_id,
        title="terminal task",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        context={
            "_review_fix_lane_id": str(lane_id),
            SPAWN_REASON_KEY: spawn_reason,
        },
    )
    session.add(task)
    await session.commit()
    return task


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_retires_parked_lane_already_merged(
    db: AsyncSession,
):
    """PARKED lane older than threshold with a terminal task carrying
    spawn_reason='already_merged' must be retired."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=7200)
    await _make_terminal_task(db, project_id, lane.id, "already_merged")

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_retires_pr_closed_unmerged(
    db: AsyncSession,
):
    """PARKED lane older than threshold with spawn_reason='pr_closed_unmerged'
    must also be retired."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=7200)
    await _make_terminal_task(db, project_id, lane.id, "pr_closed_unmerged")

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_skips_lane_with_live_chain_task(
    db: AsyncSession,
):
    """PARKED lane older than threshold but with a non-terminal (IN_PROGRESS) chain
    task must NOT be retired — the chain is still live."""
    from app.models import Task, TaskStatus
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=7200)

    live_task = Task(
        project_id=project_id,
        title="live fix task",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        status=TaskStatus.IN_PROGRESS,
        context={"_review_fix_lane_id": str(lane.id)},
    )
    db.add(live_task)
    await db.commit()

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )
    await db.commit()

    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.PARKED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_skips_recent_parked_lane(
    db: AsyncSession,
):
    """PARKED lane within the threshold window must not be touched even if
    the chain terminal spawn_reason matches."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    # parked only 60 seconds ago — well within the 3600s threshold
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=60)
    await _make_terminal_task(db, project_id, lane.id, "already_merged")

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )
    await db.commit()

    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.PARKED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_cas_miss_is_noop(db: AsyncSession):
    """A concurrent CAS miss on a PARKED lane must not raise and must not
    increment the returned count."""
    from app.improvement_lane_ops import LaneCASError
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=7200)
    await _make_terminal_task(db, project_id, lane.id, "already_merged")

    async def _raise_cas(session, lane_id, expected, next_status, **kw):
        raise LaneCASError("simulated concurrent transition")

    with (
        patch(
            "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
        ),
        patch("app.improvement_lane_ops.cas_transition", side_effect=_raise_cas),
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )

    # CAS miss is a no-op — count stays 0, lane unchanged
    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.PARKED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_retires_lane_with_failed_task(
    db: AsyncSession,
):
    """PARKED lane older than threshold whose only chain task is FAILED must be
    retired regardless of spawn_reason — all chain tasks are terminal."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=7200)
    # spawn_reason is 'spawned' — previously would have been excluded; now irrelevant.
    await _make_terminal_task(db, project_id, lane.id, "spawned")

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_retires_lane_with_no_chain_tasks(
    db: AsyncSession,
):
    """PARKED lane older than threshold with zero chain tasks must be retired —
    the chain never ran or completed without leaving a task row."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=7200)
    # No tasks at all — zero chain tasks is the empty-terminal case.

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class3_retires_lane_releases_reservation(
    db: AsyncSession,
):
    """PARKED lane retirement must release the PoolVm reservation so the member
    is immediately available to other lanes."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_parked_lane(db, project_id, parked_ago_seconds=7200)
    await _make_terminal_task(db, project_id, lane.id, "already_merged")

    member = PoolVm(
        vm_instance_id="pool-vm-class3-retire-1",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
    )
    db.add(member)
    await db.commit()

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_parked_terminal_stale_seconds": 3600},
        )
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.RETIRED.value
    await db.refresh(member)
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None


# ---------------------------------------------------------------------------
# cas_transition — terminal transitions reset workspace fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_transition_resets_workspace_state_to_cold(db: AsyncSession):
    """A lane transitioning to a terminal status (KILLED) resets workspace_state
    to COLD and clears workspace_vm_instance_id."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/ws-kill",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.workspace_state = "LEASED"
    lane.workspace_vm_instance_id = "pool-vm-ws-test-1"
    await db.commit()

    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.ACTIVE,
        ImprovementLaneStatus.KILLED,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.KILLED.value
    assert updated.workspace_state == "COLD"
    assert updated.workspace_vm_instance_id is None


@pytest.mark.asyncio
async def test_terminal_transition_retired_resets_workspace_state_to_cold(
    db: AsyncSession,
):
    """A lane transitioning to RETIRED also resets workspace_state and clears the VM ref."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/ws-retire",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    lane.workspace_state = "WARM_RESERVED"
    lane.workspace_vm_instance_id = "pool-vm-ws-test-2"
    await db.commit()

    updated = await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.PARKED,
        ImprovementLaneStatus.RETIRED,
    )
    await db.commit()

    assert updated.status == ImprovementLaneStatus.RETIRED.value
    assert updated.workspace_state == "COLD"
    assert updated.workspace_vm_instance_id is None


# ---------------------------------------------------------------------------
# clear_lane_warm_workspace_if_matching_member — covers LEASED state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_lane_warm_workspace_if_matching_member_clears_leased_state(
    db: AsyncSession,
):
    """clear_lane_warm_workspace_if_matching_member clears a lane in LEASED state,
    not just WARM_RESERVED, when the VM instance ID matches."""
    from app.warm_pool import clear_lane_warm_workspace_if_matching_member

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/ws-clear-leased",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    lane.workspace_state = "LEASED"
    lane.workspace_vm_instance_id = "pool-vm-leased-clear-1"
    await db.commit()

    cleared = await clear_lane_warm_workspace_if_matching_member(
        db, lane.id, "pool-vm-leased-clear-1"
    )
    await db.commit()

    assert cleared is True
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.workspace_state == "COLD"
    assert refreshed.workspace_vm_instance_id is None


@pytest.mark.asyncio
async def test_clear_lane_warm_workspace_if_matching_member_clears_warm_reserved(
    db: AsyncSession,
):
    """clear_lane_warm_workspace_if_matching_member still clears WARM_RESERVED lanes."""
    from app.warm_pool import clear_lane_warm_workspace_if_matching_member

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/ws-clear-reserved",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    lane.workspace_state = "WARM_RESERVED"
    lane.workspace_vm_instance_id = "pool-vm-warm-reserved-clear-1"
    await db.commit()

    cleared = await clear_lane_warm_workspace_if_matching_member(
        db, lane.id, "pool-vm-warm-reserved-clear-1"
    )
    await db.commit()

    assert cleared is True
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.workspace_state == "COLD"
    assert refreshed.workspace_vm_instance_id is None


@pytest.mark.asyncio
async def test_clear_lane_warm_workspace_if_matching_member_noop_on_cold(
    db: AsyncSession,
):
    """clear_lane_warm_workspace_if_matching_member is a no-op for COLD lanes."""
    from app.warm_pool import clear_lane_warm_workspace_if_matching_member

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/ws-cold-noop",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    lane.workspace_state = "COLD"
    lane.workspace_vm_instance_id = None
    await db.commit()

    cleared = await clear_lane_warm_workspace_if_matching_member(
        db, lane.id, "any-vm-id"
    )
    await db.commit()

    assert cleared is False
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.workspace_state == "COLD"


# ---------------------------------------------------------------------------
# activate_lane_for_fix — reservation soft-TTL re-stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reservation_ttl_restamped_at_fix_spawn(db: AsyncSession):
    """PARKED->ACTIVE transition re-stamps the PoolVm soft TTL so the fix task
    has a full window to claim it, bounded by the hard ceiling."""
    from datetime import timedelta
    from unittest.mock import patch

    from app.time_utils import _utcnow

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/restamp-soft-ttl",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    now = _utcnow()
    hard_anchor = now + timedelta(seconds=3600)
    member = PoolVm(
        vm_instance_id="pool-vm-restamp-soft-ttl",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
        # Soft TTL already expired — the fix task would miss its warm claim
        # without a re-stamp.
        reservation_expires_at=now - timedelta(seconds=60),
        reservation_hard_expires_at=hard_anchor,
    )
    db.add(member)
    await db.commit()

    task_id = uuid.uuid4()
    with patch(
        "app.shared_state.runtime_config",
        {"warm_pool_lane_reservation_ttl_seconds": 1800},
    ):
        await activate_lane_for_fix(db, lane, task_id=task_id, lease_workspace=True)
    await db.commit()

    # Lane must be ACTIVE.
    refreshed_lane = await db.get(ImprovementLane, lane.id)
    assert refreshed_lane.status == ImprovementLaneStatus.ACTIVE.value

    # Soft TTL must be extended into the future (re-stamped).
    await db.refresh(member)
    assert member.reservation_expires_at is not None
    assert member.reservation_expires_at > now, (
        "soft TTL should have been re-stamped into the future"
    )
    # Must not exceed the hard ceiling.
    assert member.reservation_expires_at <= hard_anchor, (
        "re-stamped TTL must be bounded by reservation_hard_expires_at"
    )
    # Hard ceiling itself must be unchanged.
    assert member.reservation_hard_expires_at == hard_anchor


@pytest.mark.asyncio
async def test_activate_lane_for_fix_no_restamp_when_hard_ceiling_elapsed(
    db: AsyncSession,
):
    """When the hard ceiling has already elapsed, activate_lane_for_fix does
    not re-stamp the soft TTL (clearing or extending it would be wrong — the
    maintainer's sweep owns the cleanup path)."""
    from datetime import timedelta
    from unittest.mock import patch

    from app.time_utils import _utcnow

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/restamp-hard-elapsed",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    now = _utcnow()
    original_soft = now - timedelta(seconds=120)
    original_hard = now - timedelta(seconds=30)
    member = PoolVm(
        vm_instance_id="pool-vm-restamp-hard-elapsed",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
        reservation_expires_at=original_soft,
        reservation_hard_expires_at=original_hard,
    )
    db.add(member)
    await db.commit()

    task_id = uuid.uuid4()
    with patch(
        "app.shared_state.runtime_config",
        {"warm_pool_lane_reservation_ttl_seconds": 1800},
    ):
        await activate_lane_for_fix(db, lane, task_id=task_id, lease_workspace=True)
    await db.commit()

    # Lane still transitions to ACTIVE — re-stamping is best-effort.
    refreshed_lane = await db.get(ImprovementLane, lane.id)
    assert refreshed_lane.status == ImprovementLaneStatus.ACTIVE.value

    # Soft TTL must NOT have been changed (hard ceiling elapsed).
    await db.refresh(member)
    assert member.reservation_expires_at == original_soft, (
        "soft TTL must not be re-stamped when the hard ceiling has elapsed"
    )


# ---------------------------------------------------------------------------
# activate_lane_for_fix — workspace_state branch contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_lane_for_fix_full_pilot_sets_workspace_state_leased(
    db: AsyncSession,
):
    """Full-pilot path (lease_workspace=True) must flip workspace_state to LEASED
    after PARKED->ACTIVE transition.  A stale COLD value here means the fix task
    cannot claim a warm workspace, so this is a correctness invariant."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/ws-leased-contract",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    await db.commit()

    task_id = uuid.uuid4()
    await activate_lane_for_fix(db, lane, task_id=task_id, lease_workspace=True)
    await db.commit()

    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.ACTIVE.value
    assert refreshed.workspace_state == "LEASED", (
        "full-pilot activate_lane_for_fix must set workspace_state=LEASED"
    )


@pytest.mark.asyncio
async def test_activate_lane_for_fix_shadow_mode_does_not_set_workspace_leased(
    db: AsyncSession,
):
    """Shadow path (lease_workspace=False) must NOT flip workspace_state to LEASED.
    A stray LEASED value in the shadow arm contaminates the control arm — itself a
    pilot KILL condition — so the cold state must be preserved."""
    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/ws-shadow-contract",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    await db.commit()

    task_id = uuid.uuid4()
    await activate_lane_for_fix(db, lane, task_id=task_id, lease_workspace=False)
    await db.commit()

    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.ACTIVE.value
    assert refreshed.workspace_state == "COLD", (
        "shadow activate_lane_for_fix must NOT set workspace_state=LEASED"
    )


# ---------------------------------------------------------------------------
# Reservation-expiry while lane is in LEASED state (end-to-end seam)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_reservation_clears_leased_workspace_state(
    db: AsyncSession,
):
    """When a PoolVm reservation expires while the lane is already in LEASED
    state (i.e. activate_lane_for_fix set workspace_state='LEASED' before the
    IDLE member's soft TTL lapsed), the sweep's call to
    clear_lane_warm_workspace_if_matching_member must reset workspace_state to
    COLD and clear workspace_vm_instance_id.

    The bug: the old WHERE clause used ``workspace_state == 'WARM_RESERVED'``,
    which never matched a LEASED lane, leaving the workspace pointer stale.
    The fix: ``workspace_state != 'COLD'`` covers both WARM_RESERVED and LEASED.
    """
    from datetime import timedelta

    from app.warm_pool import clear_lane_warm_workspace_if_matching_member

    project_id = await _make_project(db)
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/expired-leased",
        base_branch="main",
    )
    # Simulate state after activate_lane_for_fix: lane is ACTIVE with LEASED
    # workspace and the vm pointer set.
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.workspace_state = "LEASED"
    lane.workspace_vm_instance_id = "pool-vm-expiry-leased-1"

    # An IDLE PoolVm reserved for this lane whose soft TTL has lapsed.
    expired_at = _utcnow() - timedelta(seconds=60)
    member = PoolVm(
        vm_instance_id="pool-vm-expiry-leased-1",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
        reservation_expires_at=expired_at,
    )
    db.add(member)
    await db.commit()

    # The sweep identifies the expired IDLE member and calls
    # clear_lane_warm_workspace_if_matching_member with the PoolVm's
    # vm_instance_id.  This must clear the LEASED workspace to COLD.
    cleared = await clear_lane_warm_workspace_if_matching_member(
        db, lane.id, member.vm_instance_id
    )
    await db.commit()

    assert cleared is True, (
        "clear_lane_warm_workspace_if_matching_member must return True when "
        "the lane is in LEASED state and the vm_instance_id matches"
    )
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.workspace_state == "COLD", (
        "workspace_state must be reset to COLD after reservation expiry "
        "regardless of whether the prior state was WARM_RESERVED or LEASED"
    )
    assert refreshed.workspace_vm_instance_id is None, (
        "workspace_vm_instance_id must be cleared alongside workspace_state"
    )


# ---------------------------------------------------------------------------
# sweep_stranded_lanes — Class 1 and Class 2 coverage
# ---------------------------------------------------------------------------


async def _make_active_lane(
    session, project_id, *, current_task_id=None, phase_requeues=0
):
    """Create an ACTIVE lane with an optional task binding and phase_requeues count."""
    lane = await create_lane(
        session,
        project_id=project_id,
        direction_id=None,
        title="Active test lane",
        goal="G",
        branch_name=f"lane/active-{uuid.uuid4().hex[:8]}",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.current_task_id = current_task_id
    lane.phase_requeues = phase_requeues
    await session.commit()
    return lane


async def _make_task(session, project_id, status):
    """Create a Task row with the given TaskStatus."""
    from app.models import Task

    task = Task(
        project_id=project_id,
        title="test task",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        status=status,
    )
    session.add(task)
    await session.commit()
    return task


async def _make_proposed_lane(session, project_id, *, created_ago_seconds: int = 3600):
    """Create a PROPOSED lane whose created_at is `created_ago_seconds` in the past."""
    from datetime import timedelta

    lane = await create_lane(
        session,
        project_id=project_id,
        direction_id=None,
        title="Proposed test lane",
        goal="G",
        branch_name=f"lane/proposed-{uuid.uuid4().hex[:8]}",
        base_branch="main",
    )
    # create_lane already leaves status=PROPOSED; backdate created_at
    lane.created_at = _utcnow() - timedelta(seconds=created_ago_seconds)
    await session.commit()
    return lane


# --- Class 1: ACTIVE lane with no current_task_id ---


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_active_no_task_id_is_stranded(
    db: AsyncSession,
):
    """ACTIVE lane with current_task_id=None must be treated as stranded and parked."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_active_lane(
        db, project_id, current_task_id=None, phase_requeues=0
    )

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.KILLED.value
    assert refreshed.current_task_id is None


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_terminal_task_statuses_are_stranded(
    db: AsyncSession,
):
    """ACTIVE lane whose current_task is COMPLETED/FAILED/CANCELLED is stranded."""
    from app.models import TaskStatus
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)

    for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        task = await _make_task(db, project_id, status)
        lane = await _make_active_lane(
            db, project_id, current_task_id=task.id, phase_requeues=0
        )

        with patch(
            "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
        ):
            count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})
        await db.commit()

        assert count == 1, f"expected 1 transition for status={status}"
        refreshed = await db.get(ImprovementLane, lane.id)
        assert refreshed.status == ImprovementLaneStatus.KILLED.value, (
            f"expected KILLED for status={status} (no PR dispatch context)"
        )


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_live_task_not_stranded(
    db: AsyncSession,
):
    """ACTIVE lane whose current_task is IN_PROGRESS is NOT stranded — leave it alone."""
    from app.models import TaskStatus
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    task = await _make_task(db, project_id, TaskStatus.IN_PROGRESS)
    lane = await _make_active_lane(
        db, project_id, current_task_id=task.id, phase_requeues=0
    )

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})

    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_under_budget_no_dispatch_context_kills(
    db: AsyncSession,
):
    """Stranded ACTIVE lane under the requeue budget with no PR dispatch context
    must transition ACTIVE→KILLED (deterministic kill path clears task binding)."""
    from app.models import TaskStatus
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    task = await _make_task(db, project_id, TaskStatus.COMPLETED)
    # phase_requeues=0 → after mark_checkpoint_missing increments → 1 ≤ 3
    # but task payload has no pr_number → killed deterministically
    lane = await _make_active_lane(
        db, project_id, current_task_id=task.id, phase_requeues=0
    )

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.KILLED.value
    assert refreshed.current_task_id is None


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_over_budget_kills_and_releases_reservation(
    db: AsyncSession,
):
    """Stranded ACTIVE lane that has exhausted the requeue budget must transition
    ACTIVE→KILLED and release any pool reservation."""
    from app.models import TaskStatus
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    task = await _make_task(db, project_id, TaskStatus.FAILED)
    # phase_requeues=_LANE_MAX_PHASE_REQUEUES (3) → after increment → 4 > 3 → KILLED
    lane = await _make_active_lane(
        db, project_id, current_task_id=task.id, phase_requeues=_LANE_MAX_PHASE_REQUEUES
    )

    member = PoolVm(
        vm_instance_id=f"pool-vm-class1-kill-{uuid.uuid4().hex[:8]}",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
    )
    db.add(member)
    await db.commit()

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.KILLED.value
    await db.refresh(member)
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_cas_miss_is_noop(db: AsyncSession):
    """A concurrent CAS miss on a stranded ACTIVE lane must not raise and must
    not increment the returned count."""
    from app.improvement_lane_ops import LaneCASError
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_active_lane(
        db, project_id, current_task_id=None, phase_requeues=0
    )

    async def _raise_cas(session, lane_id, expected, next_status, **kw):
        raise LaneCASError("simulated concurrent transition")

    with (
        patch(
            "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
        ),
        patch("app.improvement_lane_ops.cas_transition", side_effect=_raise_cas),
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})

    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_inert_when_disabled(db: AsyncSession):
    """When improvement_lanes_enabled=False, Class 1 sweep must be a no-op."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    await _make_active_lane(db, project_id, current_task_id=None, phase_requeues=0)

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=False
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})

    assert count == 0


# --- Class 2: stale PROPOSED lanes ---


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class2_stale_proposed_lane_is_killed(
    db: AsyncSession,
):
    """PROPOSED lane older than the staleness threshold must be killed."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    # Created 3600s ago; threshold is 1800s (default) — well past the threshold
    lane = await _make_proposed_lane(db, project_id, created_ago_seconds=3600)

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_proposed_stale_seconds": 1800},
        )
    await db.commit()

    assert count == 1
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.KILLED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class2_recent_proposed_lane_left_alone(
    db: AsyncSession,
):
    """PROPOSED lane newer than the staleness threshold must be left unchanged."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    # Created 60s ago; threshold is 1800s — well within the window
    lane = await _make_proposed_lane(db, project_id, created_ago_seconds=60)

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_proposed_stale_seconds": 1800},
        )

    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.PROPOSED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class2_cas_miss_is_noop(db: AsyncSession):
    """A concurrent CAS miss on a stale PROPOSED lane must not raise and must
    not increment the returned count."""
    from app.improvement_lane_ops import LaneCASError
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_proposed_lane(db, project_id, created_ago_seconds=3600)

    async def _raise_cas(session, lane_id, expected, next_status, **kw):
        raise LaneCASError("simulated concurrent transition")

    with (
        patch(
            "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
        ),
        patch("app.improvement_lane_ops.cas_transition", side_effect=_raise_cas),
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_proposed_stale_seconds": 1800},
        )

    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.PROPOSED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class2_inert_when_disabled(db: AsyncSession):
    """When improvement_lanes_enabled=False, Class 2 sweep must be a no-op."""
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)
    lane = await _make_proposed_lane(db, project_id, created_ago_seconds=3600)

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=False
    ):
        count = await sweep_stranded_lanes(
            db,
            _utcnow(),
            runtime_config={"improvement_lane_proposed_stale_seconds": 1800},
        )

    assert count == 0
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.PROPOSED.value


# ---------------------------------------------------------------------------
# checkpoint_and_reserve_on_phase_complete — cost/token accumulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_accumulates_phase_cost_and_tokens(db: AsyncSession):
    """checkpoint_and_reserve_on_phase_complete must forward the completing
    phase task's banked tokens and VM cost into the LaneCheckpointPayload so
    that checkpoint_lane accumulates them onto the lane row totals.

    Before this fix the payload was built with tokens_in=0/tokens_out=0/
    vm_cost_usd=0.0, leaving llm_input_tokens_total, llm_output_tokens_total,
    and vm_cost_usd_total permanently zero for every lane.
    """
    from unittest.mock import AsyncMock, patch

    from app.improvement_lane_ops import cas_transition
    from app.models import ImprovementLaneStatus, Task, TaskStatus
    from app.review_fix_lane_pilot import checkpoint_and_reserve_on_phase_complete

    project_id = await _make_project(db)

    # Create an ACTIVE lane.
    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id=None,
        title="cost-accounting lane",
        goal="verify cost accumulation",
        branch_name="lane/cost-acct",
        base_branch="main",
    )

    # Create a task with known banked accounting values.
    task = Task(
        project_id=project_id,
        title="phase task",
        payloads=[{"type": "fix", "prompt": "fix it", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        llm_input_tokens_banked_db=1200,
        llm_output_tokens_banked_db=400,
        vm_runtime_cost_banked_db=0.08,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    # Transition lane to ACTIVE, bound to our task.
    await cas_transition(
        db,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=task.id,
    )
    await db.commit()
    await db.refresh(lane)
    await db.refresh(task)

    # Mock GitHub SHA verification to return reachable=True (happy path).
    with (
        patch(
            "app.github_token_resolver.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("gh-token", "owner", "repo"),
        ),
        patch(
            "app.github_refs.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await checkpoint_and_reserve_on_phase_complete(
            db,
            task,
            lane,
            sha="abc1234def5678",
            summary="phase complete",
            reserve_workspace=False,
        )

    assert result is True

    await db.commit()
    await db.refresh(lane)
    assert lane.llm_input_tokens_total == 1200
    assert lane.llm_output_tokens_total == 400
    assert abs(lane.vm_cost_usd_total - 0.08) < 1e-6


# ---------------------------------------------------------------------------
# rebind_lane_to_recovery — releases reserved PoolVm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_rebind_releases_reserved_vm(db: AsyncSession):
    """rebind_lane_to_recovery must release any PoolVm reserved for the lane.

    When a lane drops to checkpoint recovery the workspace becomes COLD and the
    recovery task cold-dispatches, so the previously-reserved warm VM is no
    longer needed. Leaving it reserved would silently consume warm-pool capacity
    until the hard-TTL sweep expired it.
    """
    from datetime import timedelta

    from app.review_fix_lane_pilot import rebind_lane_to_recovery
    from app.time_utils import _utcnow

    project_id = await _make_project(db)
    lane = await _make_active_lane(
        db, project_id, current_task_id=None, phase_requeues=1
    )

    now = _utcnow()
    member = PoolVm(
        vm_instance_id=f"pool-vm-rebind-{uuid.uuid4().hex[:8]}",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
        reservation_expires_at=now + timedelta(seconds=900),
        reservation_hard_expires_at=now + timedelta(seconds=3600),
    )
    db.add(member)
    await db.commit()

    recovery_task_id = uuid.uuid4()
    await rebind_lane_to_recovery(
        db,
        lane,
        task_id=recovery_task_id,
        reason="checkpoint_verification_failed",
    )
    await db.commit()

    await db.refresh(lane)
    await db.refresh(member)

    assert lane.workspace_state == "COLD"
    assert lane.current_task_id == recovery_task_id
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None
    assert member.reservation_hard_expires_at is None


# ---------------------------------------------------------------------------
# checkpoint_or_recover_phase — operator board emit on RECOVER path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_or_recover_phase_emits_pre_exhaustion_operator_message(
    db: AsyncSession,
):
    """RECOVER path must post a ProjectMessage so the operator board shows the miss.

    Previously only logger.warning was emitted; this verifies that
    emit_lane_event_message now writes a deduplicated ProjectMessage with
    event_type='lane_checkpoint_recover' when the checkpoint budget is not yet
    exhausted.
    """
    from app.models import ProjectMessage, TaskStatus
    from app.review_fix_lane_pilot import (
        CHECKPOINT_PHASE_RECOVER,
        checkpoint_or_recover_phase,
    )

    project_id = await _make_project(db)
    task = await _make_task(db, project_id, TaskStatus.EVALUATING)
    lane = await _make_active_lane(
        db, project_id, current_task_id=task.id, phase_requeues=0
    )

    # sha=None triggers mark_checkpoint_missing (miss recorded, budget intact → RECOVER)
    result = await checkpoint_or_recover_phase(
        db,
        task,
        lane,
        sha=None,
        summary="test summary",
        reserve_workspace=False,
        enable_recovery=True,
    )
    await db.flush()

    assert result == CHECKPOINT_PHASE_RECOVER

    msgs = (
        (
            await db.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    recover_msgs = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "lane_checkpoint_recover"
    ]
    assert len(recover_msgs) == 1, (
        f"expected 1 lane_checkpoint_recover message, got {len(recover_msgs)}: "
        f"{[m.content for m in msgs]}"
    )
    assert (
        str(lane.id) in recover_msgs[0].content or lane.title in recover_msgs[0].content
    )


@pytest.mark.asyncio
async def test_checkpoint_or_recover_phase_emits_pre_exhaustion_budget_alert(
    db: AsyncSession,
):
    """At penultimate requeue slot, a lane_budget_near_exhausted message must be emitted.

    Start a lane with phase_requeues = _LANE_MAX_PHASE_REQUEUES - 2 so that after one
    checkpoint miss it reaches _LANE_MAX_PHASE_REQUEUES - 1 (the penultimate slot).
    The function must emit both the regular lane_checkpoint_recover message AND a
    distinct lane_budget_near_exhausted message.
    """
    from app.models import ProjectMessage, TaskStatus, _LANE_MAX_PHASE_REQUEUES
    from app.review_fix_lane_pilot import (
        CHECKPOINT_PHASE_RECOVER,
        checkpoint_or_recover_phase,
    )

    project_id = await _make_project(db)
    task = await _make_task(db, project_id, TaskStatus.EVALUATING)
    lane = await _make_active_lane(
        db,
        project_id,
        current_task_id=task.id,
        phase_requeues=_LANE_MAX_PHASE_REQUEUES - 2,
    )

    result = await checkpoint_or_recover_phase(
        db,
        task,
        lane,
        sha=None,
        summary="test summary",
        reserve_workspace=False,
        enable_recovery=True,
    )
    await db.flush()

    assert result == CHECKPOINT_PHASE_RECOVER

    msgs = (
        (
            await db.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    event_types = [(m.context or {}).get("event_type") for m in msgs]
    assert "lane_checkpoint_recover" in event_types, (
        f"expected lane_checkpoint_recover message, got event_types={event_types}"
    )
    assert "lane_budget_near_exhausted" in event_types, (
        f"expected lane_budget_near_exhausted message at penultimate slot, got event_types={event_types}"
    )
    near_exhausted = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "lane_budget_near_exhausted"
    ]
    assert len(near_exhausted) == 1
    assert "next checkpoint miss will kill" in near_exhausted[0].content


# ---------------------------------------------------------------------------
# sweep_stranded_lanes — operator board emit on recovery dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_recovery_dispatch_emits_operator_message(
    db: AsyncSession,
):
    """Stranded ACTIVE lane with dispatch context spawns a recovery task AND emits
    a ProjectMessage on the operator board."""
    from app.models import ProjectMessage, Task, TaskStatus
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)

    # Task with pr_number in payload so the recovery-dispatch branch is taken
    task = Task(
        project_id=project_id,
        title="fix PR 42",
        payloads=[
            {
                "type": "fix",
                "prompt": "fix the issue",
                "pr_number": 42,
                "branch": "feat/test-branch",
                "base_branch": "main",
            }
        ],
        status=TaskStatus.FAILED,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    lane = await _make_active_lane(
        db, project_id, current_task_id=task.id, phase_requeues=0
    )
    lane_id = lane.id

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})
    await db.commit()

    assert count == 1

    msgs = (
        (
            await db.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    recover_msgs = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "lane_checkpoint_recover"
    ]
    assert len(recover_msgs) == 1, (
        f"expected 1 lane_checkpoint_recover message, got {len(recover_msgs)}: "
        f"{[m.content for m in msgs]}"
    )
    assert str(lane_id) in (recover_msgs[0].context or {}).get("dedupe_key", "")


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_class1_recovery_task_carries_lane_chain_id(
    db: AsyncSession,
):
    """Recovery task spawned by Class-1 sweep must carry LANE_CHAIN_ID_KEY equal to
    lane.id even when the stranded task's context lacked that key."""
    from app.models import Task, TaskStatus
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY
    from app.warm_pool_maintainer import sweep_stranded_lanes

    project_id = await _make_project(db)

    # Stranded task has NO _review_fix_lane_id in its context
    task = Task(
        project_id=project_id,
        title="fix PR 99",
        payloads=[
            {
                "type": "fix",
                "prompt": "fix it",
                "pr_number": 99,
                "branch": "feat/no-chain-id",
                "base_branch": "main",
            }
        ],
        status=TaskStatus.FAILED,
        context={},  # deliberately empty — no LANE_CHAIN_ID_KEY
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    lane = await _make_active_lane(
        db, project_id, current_task_id=task.id, phase_requeues=0
    )
    lane_id = lane.id

    with patch(
        "app.runtime_config_state.is_improvement_lanes_enabled", return_value=True
    ):
        count = await sweep_stranded_lanes(db, _utcnow(), runtime_config={})
    await db.commit()

    assert count == 1

    refreshed_lane = await db.get(ImprovementLane, lane_id)
    assert refreshed_lane.status == ImprovementLaneStatus.ACTIVE.value

    recovery_task = await db.get(Task, refreshed_lane.current_task_id)
    assert recovery_task is not None
    assert (recovery_task.context or {}).get(LANE_CHAIN_ID_KEY) == str(lane_id)


# ---------------------------------------------------------------------------
# TestImprovementLaneDetail — GET /api/admin/improvement-lanes/{lane_id}
# ---------------------------------------------------------------------------


def _make_lane_detail(project_id, **kwargs):
    """Minimal ImprovementLane factory for detail endpoint tests."""
    defaults = dict(
        project_id=project_id,
        direction_id="d-detail123",
        title="Detail lane",
        goal="Improve detail",
        branch_name="lane/detail-branch",
        base_branch="main",
        status=ImprovementLaneStatus.PARKED.value,
        checkpoint_seq=0,
        checkpoints=[],
        manifest={},
        workspace_state="COLD",
        phase_requeues=0,
        llm_input_tokens_total=0,
        llm_output_tokens_total=0,
        vm_cost_usd_total=0.0,
        reserved_idle_cost_usd_total=0.0,
    )
    defaults.update(kwargs)
    return ImprovementLane(**defaults)


class TestImprovementLaneDetail:
    """Tests for GET /api/admin/improvement-lanes/{lane_id}."""

    @pytest.mark.asyncio
    async def test_404_on_unknown_lane_id(self, client: AsyncClient, db_session):
        """Returns 404 when the lane_id does not exist."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        unknown_id = uuid.uuid4()
        resp = await client.get(
            f"/api/admin/improvement-lanes/{unknown_id}?project_id={project.id}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cross_project_returns_404(self, client: AsyncClient, db_session):
        """Returns 404 when the lane belongs to project A but project B's id is given."""
        project_a = make_project(name="Project A")
        project_b = make_project(name="Project B")
        db_session.add_all([project_a, project_b])
        await db_session.commit()

        lane = _make_lane_detail(project_a.id)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project_b.id}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_identity_and_timeline_fields(
        self, client: AsyncClient, db_session
    ):
        """Returns lane identity and timeline fields."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane_detail(
            project.id,
            title="My lane",
            goal="My goal",
            direction_id="d-abc",
            version_number=5,
            status=ImprovementLaneStatus.ACTIVE.value,
            branch_name="lane/my-branch",
            base_branch="develop",
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["id"] == str(lane.id)
        assert body["title"] == "My lane"
        assert body["goal"] == "My goal"
        assert body["direction_id"] == "d-abc"
        assert body["version_number"] == 5
        assert body["status"] == ImprovementLaneStatus.ACTIVE.value
        assert body["branch_name"] == "lane/my-branch"
        assert body["base_branch"] == "develop"
        assert body["created_at"] is not None
        assert body["updated_at"] is not None
        assert body["parked_at"] is None
        assert body["last_resumed_at"] is None
        assert body["retired_at"] is None

    @pytest.mark.asyncio
    async def test_returns_full_checkpoints_array(
        self, client: AsyncClient, db_session
    ):
        """Returns the full checkpoints[] array as stored on the lane row."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        checkpoints = [
            {
                "seq": 1,
                "phase": "review",
                "sha": "aaa111",
                "parent_sha": None,
                "created_at": "2026-01-01T00:00:00",
                "summary": "First checkpoint",
                "validated": {"lint": True, "tests": True},
                "wip_failing": False,
                "tokens_in": 100,
                "tokens_out": 50,
                "vm_cost_usd": 0.01,
                "warm_resume": False,
            },
            {
                "seq": 2,
                "phase": "fix",
                "sha": "bbb222",
                "parent_sha": "aaa111",
                "created_at": "2026-01-01T01:00:00",
                "summary": "Second checkpoint",
                "validated": {"lint": True, "tests": False},
                "wip_failing": True,
                "tokens_in": 200,
                "tokens_out": 80,
                "vm_cost_usd": 0.02,
                "warm_resume": True,
            },
        ]
        lane = _make_lane_detail(
            project.id,
            checkpoint_seq=2,
            checkpoints=checkpoints,
            last_checkpoint_sha="bbb222",
            last_checkpoint_phase="fix",
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["checkpoint_seq"] == 2
        assert body["last_checkpoint_sha"] == "bbb222"
        assert body["last_checkpoint_phase"] == "fix"
        assert len(body["checkpoints"]) == 2
        assert body["checkpoints"][0]["sha"] == "aaa111"
        assert body["checkpoints"][1]["sha"] == "bbb222"
        assert body["checkpoints"][1]["warm_resume"] is True

    @pytest.mark.asyncio
    async def test_returns_full_manifest_dict(self, client: AsyncClient, db_session):
        """Returns the full manifest dict as stored on the lane row."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        manifest = {
            "phase_log": ["review", "fix"],
            "remaining": ["test coverage"],
            "critical_files": ["app/auth.py"],
            "failing_tests": ["test_auth.py::test_login"],
            "risks": ["auth latency regression"],
            "progress": "50%",
            "recovery": {"reason": "checkpoint_missing", "attempt": 1},
        }
        lane = _make_lane_detail(project.id, manifest=manifest)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["manifest"]["phase_log"] == ["review", "fix"]
        assert body["manifest"]["critical_files"] == ["app/auth.py"]
        assert body["manifest"]["recovery"]["reason"] == "checkpoint_missing"

    @pytest.mark.asyncio
    async def test_returns_workspace_and_accounting_fields(
        self, client: AsyncClient, db_session
    ):
        """Returns workspace state and accounting rollup fields."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane_detail(
            project.id,
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="vm-123",
            pilot_arm="lane",
            llm_input_tokens_total=1000,
            llm_output_tokens_total=500,
            vm_cost_usd_total=0.25,
            reserved_idle_cost_usd_total=0.05,
            reservation_claimed_by_fix_count=2,
            reservation_expired_before_fix_count=1,
            reservation_force_expired_count=0,
            phase_requeues=1,
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["workspace_state"] == "WARM_RESERVED"
        assert body["workspace_vm_instance_id"] == "vm-123"
        assert body["pilot_arm"] == "lane"
        assert body["llm_input_tokens_total"] == 1000
        assert body["llm_output_tokens_total"] == 500
        assert abs(body["vm_cost_usd_total"] - 0.25) < 1e-5
        assert abs(body["reserved_idle_cost_usd_total"] - 0.05) < 1e-5
        assert body["reservation_claimed_by_fix_count"] == 2
        assert body["reservation_expired_before_fix_count"] == 1
        assert body["reservation_force_expired_count"] == 0
        assert body["phase_requeues"] == 1

    @pytest.mark.asyncio
    async def test_current_task_populated_when_set(
        self, client: AsyncClient, db_session
    ):
        """Returns resolved current_task when current_task_id is non-null."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        task = make_task(
            project.id,
            title="Fix PR 42",
            payloads=[{"type": "fix", "pr_number": 42}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        lane = _make_lane_detail(
            project.id,
            status=ImprovementLaneStatus.ACTIVE.value,
            current_task_id=task.id,
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["current_task_id"] == str(task.id)
        assert body["current_task"] is not None
        assert body["current_task"]["task_id"] == str(task.id)
        assert body["current_task"]["title"] == "Fix PR 42"
        assert body["current_task"]["task_type"] == "fix"
        assert body["current_task"]["status"] is not None

    @pytest.mark.asyncio
    async def test_current_task_null_when_not_set(
        self, client: AsyncClient, db_session
    ):
        """Returns current_task=null when current_task_id is null (idle/parked lane)."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane_detail(project.id, current_task_id=None)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["current_task_id"] is None
        assert body["current_task"] is None

    @pytest.mark.asyncio
    async def test_at_risk_false_when_phase_requeues_below_threshold(
        self, client: AsyncClient, db_session
    ):
        """at_risk=False when phase_requeues < _LANE_MAX_PHASE_REQUEUES - 1."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane_detail(project.id, phase_requeues=1)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase_requeues"] == 1
        assert body["at_risk"] is False

    @pytest.mark.asyncio
    async def test_at_risk_true_when_phase_requeues_at_threshold(
        self, client: AsyncClient, db_session
    ):
        """at_risk=True when phase_requeues >= _LANE_MAX_PHASE_REQUEUES - 1 (i.e., >= 2)."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Test at threshold (phase_requeues = 2, which is _LANE_MAX_PHASE_REQUEUES - 1)
        lane = _make_lane_detail(project.id, phase_requeues=2)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase_requeues"] == 2
        assert body["at_risk"] is True

    @pytest.mark.asyncio
    async def test_at_risk_true_when_phase_requeues_above_threshold(
        self, client: AsyncClient, db_session
    ):
        """at_risk=True when phase_requeues > _LANE_MAX_PHASE_REQUEUES - 1."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Test above threshold (phase_requeues = 3, which is _LANE_MAX_PHASE_REQUEUES)
        lane = _make_lane_detail(project.id, phase_requeues=3)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes/{lane.id}?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase_requeues"] == 3
        assert body["at_risk"] is True


# ---------------------------------------------------------------------------
# TestImprovementLaneDrain
# ---------------------------------------------------------------------------


class TestImprovementLaneDrain:
    """Tests for kill_single_lane: operator kill of a single stuck lane."""

    @pytest.mark.asyncio
    async def test_kill_single_lane_transitions_to_killed_and_releases_reservation(
        self, db: AsyncSession
    ):
        """kill_single_lane should CAS an ACTIVE lane to KILLED and release any
        PoolVm reservation so the member rejoins the general pool immediately."""
        from app.review_fix_lane_pilot import kill_single_lane
        from app.time_utils import _utcnow

        project_id = await _make_project(db)
        lane = await create_lane(
            db,
            project_id=project_id,
            direction_id="d-kill1",
            title="Kill single lane",
            goal="Test operator kill",
            branch_name="lane/kill1",
            base_branch="main",
        )
        lane.status = ImprovementLaneStatus.ACTIVE.value
        # A warm-pool member reserved for this lane.
        member = PoolVm(
            vm_instance_id="pool-vm-kill-test-1",
            project_id=project_id,
            state=PoolVmState.IDLE,
            reserved_lane_id=lane.id,
            reservation_expires_at=_utcnow(),
            reservation_hard_expires_at=_utcnow(),
        )
        db.add(member)
        await db.commit()

        report = await kill_single_lane(db, lane.id)
        await db.commit()

        assert report["lanes_killed"] == 1
        assert report["reservations_cleared"] == 1
        refreshed = await db.get(ImprovementLane, lane.id)
        assert refreshed.status == ImprovementLaneStatus.KILLED.value
        # Reservation must be released so the member is immediately available.
        await db.refresh(member)
        assert member.reserved_lane_id is None
        assert member.reservation_expires_at is None
        assert member.reservation_hard_expires_at is None


@pytest.mark.asyncio
async def test_orphaned_lane_workspace_reclaimed_when_member_torn_down(
    db: AsyncSession, mocker
):
    """retire_and_delete_pool_vm must clear workspace_state and
    workspace_vm_instance_id on any lane whose backing VM is torn down,
    regardless of the teardown reason (drain, cost-guard, idle-reaper, etc.)."""
    from unittest.mock import patch

    from app.warm_pool_maintainer import retire_and_delete_pool_vm

    project_id = await _make_project(db)

    lane = await create_lane(
        db,
        project_id=project_id,
        direction_id="d-orphan-vm-test",
        title="Orphan workspace test",
        goal="Verify workspace cleared on VM teardown",
        branch_name="lane/orphan-vm",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    lane.workspace_state = "WARM_RESERVED"
    lane.workspace_vm_instance_id = "pool-vm-orphan-teardown-1"
    db.add(lane)

    member = PoolVm(
        vm_instance_id="pool-vm-orphan-teardown-1",
        project_id=project_id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
    )
    db.add(member)
    await db.commit()

    with patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock):
        ok = await retire_and_delete_pool_vm(
            db, member, reason="pool_idle_ttl", claim_grace_seconds=0
        )

    assert ok is True
    # Pool VM row must be gone.
    assert await db.get(PoolVm, member.id) is None
    # Lane workspace state must be cleared — no dangling reference to deleted VM.
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.workspace_state == "COLD"
    assert refreshed.workspace_vm_instance_id is None


# ---------------------------------------------------------------------------
# lane control-plane durability across simulated orchestrator restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_control_plane_survives_simulated_restart():
    """Lane control-plane state (checkpoints, manifest, accounting, workspace)
    reconstructs exactly from DB across a simulated orchestrator restart (session
    close + reopen).  Also verifies that a lane whose backing pool VM vanished
    during downtime is reclaimed to COLD by reclaim_orphaned_lane_workspace_states.
    """
    from app.improvement_lane_ops import reclaim_orphaned_lane_workspace_states
    from app.models import Project

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

    # ── Phase 1: build full lane state and commit ──────────────────────────
    vm_instance_id = "pool-vm-restart-test-1"
    lane_id = None
    vm_pk = None

    async with Session() as s1:
        project_id = uuid.uuid4()
        s1.add(
            Project(
                id=project_id,
                user_id="test-user",
                name="test-project",
                github_owner="owner",
                github_repo="repo",
                status="DEPLOYING",
            )
        )
        await s1.commit()

        lane = await create_lane(
            s1,
            project_id=project_id,
            direction_id="d-restart-test",
            title="Restart durability test",
            goal="Verify DB state survives session close",
            branch_name="lane/restart-test",
            base_branch="main",
        )
        task_id = uuid.uuid4()
        lane.status = ImprovementLaneStatus.ACTIVE.value
        lane.current_task_id = task_id
        await s1.commit()

        cp = LaneCheckpointPayload(
            sha="restart-sha-abc1234",
            summary="pre-restart checkpoint",
            validated=LaneCheckpointValidated(lint=True, tests="green"),
            manifest_patch={"progress": ["restart test pass"]},
            tokens_in=500,
            tokens_out=250,
            vm_cost_usd=0.02,
        )
        await checkpoint_lane(s1, lane.id, "create", task_id, cp)
        await s1.commit()

        # Attach a PoolVm reservation and record WARM_RESERVED workspace state.
        member = PoolVm(
            vm_instance_id=vm_instance_id,
            project_id=project_id,
            state=PoolVmState.IDLE,
            reserved_lane_id=lane.id,
        )
        s1.add(member)
        lane_row = await s1.get(ImprovementLane, lane.id)
        lane_row.workspace_state = "WARM_RESERVED"
        lane_row.workspace_vm_instance_id = vm_instance_id
        await s1.commit()

        lane_id = lane.id
        vm_pk = member.id
    # s1 is closed here — simulates process exit / orchestrator restart.

    # ── Phase 2: fresh session reconstructs control-plane state from DB ───
    async with Session() as s2:
        recovered = await s2.get(ImprovementLane, lane_id)

        assert recovered is not None, "lane must persist across session close"
        assert recovered.status == ImprovementLaneStatus.PARKED.value
        assert recovered.checkpoint_seq == 1
        assert recovered.last_checkpoint_sha == "restart-sha-abc1234"
        assert recovered.last_checkpoint_phase == "create"
        assert len(recovered.checkpoints) == 1
        assert recovered.checkpoints[0]["sha"] == "restart-sha-abc1234"
        assert recovered.checkpoints[0]["validated"]["tests"] == "green"
        assert "restart test pass" in recovered.manifest.get("progress", [])
        assert recovered.llm_input_tokens_total == 500
        assert recovered.llm_output_tokens_total == 250
        assert abs(recovered.vm_cost_usd_total - 0.02) < 1e-6
        assert recovered.workspace_state == "WARM_RESERVED"
        assert recovered.workspace_vm_instance_id == vm_instance_id

        # Simulate the pool VM being torn down during downtime (crash / external
        # deletion bypassing retire_and_delete_pool_vm).
        vm_row = await s2.get(PoolVm, vm_pk)
        await s2.delete(vm_row)
        await s2.commit()

        # Startup recovery sweep must reclaim the dangling workspace pointer.
        reclaimed = await reclaim_orphaned_lane_workspace_states(s2)
        await s2.commit()

        assert reclaimed == 1
        await s2.refresh(recovered)
        assert recovered.workspace_state == "COLD", (
            "workspace_state must be reclaimed to COLD when backing VM is gone"
        )
        assert recovered.workspace_vm_instance_id is None

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# POST /api/admin/improvement-lanes/reclaim-orphaned — endpoint test
# ---------------------------------------------------------------------------


class TestReclaimOrphanedLaneWorkspaces:
    """Tests for POST /api/admin/improvement-lanes/reclaim-orphaned."""

    @pytest.mark.asyncio
    async def test_reclaim_orphaned_returns_count(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """POST reclaim-orphaned returns reclaimed=N for N orphaned lanes and
        resets their workspace_state to COLD."""
        from app.models import Project

        project_id = uuid.uuid4()
        db_session.add(
            Project(
                id=project_id,
                user_id="test-user",
                name="reclaim-test-project",
                github_owner="owner",
                github_repo="repo",
                status="DEPLOYING",
            )
        )
        await db_session.commit()

        # Lane with WARM_RESERVED state but no backing PoolVm — an orphan.
        orphan_lane = ImprovementLane(
            project_id=project_id,
            direction_id="d-reclaim-test",
            title="Orphan lane",
            goal="Test reclaim endpoint",
            branch_name="lane/reclaim-test",
            base_branch="main",
            status=ImprovementLaneStatus.ACTIVE.value,
            checkpoint_seq=0,
            checkpoints=[],
            manifest={},
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="ghost-vm-reclaim-1",
            phase_requeues=0,
        )
        # Lane already COLD — must not be counted.
        cold_lane = ImprovementLane(
            project_id=project_id,
            direction_id="d-reclaim-test-cold",
            title="Cold lane",
            goal="Should not be reclaimed",
            branch_name="lane/reclaim-test-cold",
            base_branch="main",
            status=ImprovementLaneStatus.PARKED.value,
            checkpoint_seq=0,
            checkpoints=[],
            manifest={},
            workspace_state="COLD",
            phase_requeues=0,
        )
        db_session.add_all([orphan_lane, cold_lane])
        await db_session.commit()

        resp = await client.post("/api/admin/improvement-lanes/reclaim-orphaned")

        assert resp.status_code == 200
        body = resp.json()
        assert body["reclaimed"] == 1

        await db_session.refresh(orphan_lane)
        assert orphan_lane.workspace_state == "COLD"
        assert orphan_lane.workspace_vm_instance_id is None

    @pytest.mark.asyncio
    async def test_reclaim_orphaned_no_orphans_returns_zero(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Returns reclaimed=0 when no orphaned lanes exist (idempotent)."""
        resp = await client.post("/api/admin/improvement-lanes/reclaim-orphaned")
        assert resp.status_code == 200
        assert resp.json()["reclaimed"] == 0

    @pytest.mark.asyncio
    async def test_reclaim_orphaned_dry_run_reports_without_mutating(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """dry_run=true returns structured counts without clearing workspace state."""
        from app.models import Project

        project_id = uuid.uuid4()
        db_session.add(
            Project(
                id=project_id,
                user_id="test-user",
                name="dry-run-reclaim-project",
                github_owner="owner",
                github_repo="repo",
                status="DEPLOYING",
            )
        )
        await db_session.flush()
        lane = ImprovementLane(
            project_id=project_id,
            direction_id="d-reclaim-dry-run",
            title="Dry run orphan lane",
            goal="Test dry-run endpoint",
            branch_name="lane/reclaim-dry-run",
            base_branch="main",
            status=ImprovementLaneStatus.ACTIVE.value,
            checkpoint_seq=0,
            checkpoints=[],
            manifest={},
            workspace_state="LEASED",
            workspace_vm_instance_id="ghost-vm-reclaim-dry-run",
            phase_requeues=0,
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/reclaim-orphaned?dry_run=true&project_id={project_id}"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["reclaimed"] == 0
        assert body["would_reclaim"] == 1
        assert body["missing_backing_vm"] == 1

        await db_session.refresh(lane)
        assert lane.workspace_state == "LEASED"
        assert lane.workspace_vm_instance_id == "ghost-vm-reclaim-dry-run"
