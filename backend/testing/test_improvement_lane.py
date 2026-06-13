"""Tests for improvement_lane_ops: CAS transitions, checkpoint enforcement."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

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
    lane.checkpoints = [
        {"seq": i, "sha": f"sha{i:04d}"} for i in range(_LANE_CHECKPOINT_LIMIT)
    ]
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

    stale_task_id = uuid.uuid4()
    cp = LaneCheckpointPayload(sha="stale_sha", summary="stale checkpoint attempt")
    with pytest.raises(LaneOwnerMismatchError, match=str(owner_id)):
        await checkpoint_lane(db, lane.id, "create", stale_task_id, cp)

    # Verify the lane is untouched — the DB session may have partial state,
    # so re-fetch to confirm.
    await db.rollback()
    refreshed = await db.get(ImprovementLane, lane.id)
    assert refreshed.status == ImprovementLaneStatus.ACTIVE.value
    assert refreshed.current_task_id == owner_id
    assert refreshed.last_checkpoint_sha == "original_sha"


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
