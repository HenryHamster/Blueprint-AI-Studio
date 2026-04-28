"""Tests for drift_detection.compute_direction_drift.

Synthetic claims-vs-shipped fixtures verify:
 - healthy direction: high consistency, no flags
 - over-claimer: many claims, few merges (over_claim_flag set)
 - stalled-despite-churn: claims but zero merges (stalled flag set)
 - no-data direction: returns neutral signal, not error
 - per-system rollup correctness across multiple directions
 - fresh PRs older than the window are excluded
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.drift_detection import compute_direction_drift
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.time_utils import _utcnow


def _direction(d_id: str, system_id: str | None = None) -> dict:
    return {
        "id": d_id,
        "text": f"Direction {d_id}",
        "status": "active",
        "priority": "medium",
        **({"system_id": system_id} if system_id else {}),
    }


async def _make_completed_task(
    db_session,
    project: Project,
    *,
    direction_id: str,
    pr_merged: bool,
    has_pr_artifact: bool,
    age_hours: int = 1,
    plan_output: dict | None = None,
) -> Task:
    """Create a COMPLETED task with the given claim/merge signals."""
    ctx: dict = {"direction_id": direction_id}
    if pr_merged:
        ctx["pr_merged"] = True
    task = Task(
        project_id=project.id,
        title="Test claim task",
        description="",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[{"type": "create", "prompt": "test", "current_direction_id": direction_id}],
        context=ctx,
        plan_output=plan_output or {"summary": "Shipped a thing"},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=_utcnow() - timedelta(hours=age_hours),
    )
    db_session.add(task)
    await db_session.flush()
    if has_pr_artifact:
        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url=f"https://github.com/test/repo/pull/{task.id.hex[:6]}",
        )
        db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.fixture
async def project_with_directions(db_session):
    """Project with three directions, two systems."""
    project = Project(
        name="Drift Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            _direction("d-healthy", system_id="sys-alpha"),
            _direction("d-overclaim", system_id="sys-alpha"),
            _direction("d-stalled", system_id="sys-beta"),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ── Test 1: Healthy direction — high consistency, no flags ─────────────────

@pytest.mark.asyncio
async def test_healthy_direction_returns_high_consistency_no_flags(
    db_session, project_with_directions,
):
    """4 claims + 4 merged PRs → consistency=1.0, no flags."""
    project = project_with_directions
    for _ in range(4):
        await _make_completed_task(
            db_session, project,
            direction_id="d-healthy",
            pr_merged=True, has_pr_artifact=True,
        )

    drift = await compute_direction_drift(db_session, project)
    sig = drift["by_direction"]["d-healthy"]
    assert sig["claimed_changes_count"] == 4
    assert sig["actually_merged_prs"] == 4
    assert sig["claim_shipped_consistency"] == 1.0
    assert sig["over_claim_flag"] is False
    assert sig["stalled_despite_churn_flag"] is False


# ── Test 2: Over-claimer — many claims, few merges → over_claim_flag ───────

@pytest.mark.asyncio
async def test_over_claimer_sets_over_claim_flag(
    db_session, project_with_directions,
):
    """5 claims + 1 merged → consistency=0.2, over_claim_flag set."""
    project = project_with_directions
    # 5 tasks claim shipped, only 1 has merged PR
    await _make_completed_task(
        db_session, project, direction_id="d-overclaim",
        pr_merged=True, has_pr_artifact=True,
    )
    for _ in range(4):
        await _make_completed_task(
            db_session, project, direction_id="d-overclaim",
            pr_merged=False, has_pr_artifact=False,
        )

    drift = await compute_direction_drift(db_session, project)
    sig = drift["by_direction"]["d-overclaim"]
    assert sig["claimed_changes_count"] == 5
    assert sig["actually_merged_prs"] == 1
    assert sig["claim_shipped_consistency"] == 0.2
    assert sig["over_claim_flag"] is True
    assert sig["stalled_despite_churn_flag"] is False


# ── Test 3: Stalled-despite-churn — claims but zero merges ────────────────

@pytest.mark.asyncio
async def test_stalled_direction_sets_stalled_flag(
    db_session, project_with_directions,
):
    """5 claims + 0 merged → stalled_despite_churn_flag set."""
    project = project_with_directions
    for _ in range(5):
        await _make_completed_task(
            db_session, project, direction_id="d-stalled",
            pr_merged=False, has_pr_artifact=False,
        )

    drift = await compute_direction_drift(db_session, project)
    sig = drift["by_direction"]["d-stalled"]
    assert sig["claimed_changes_count"] == 5
    assert sig["actually_merged_prs"] == 0
    assert sig["claim_shipped_consistency"] == 0.0
    assert sig["stalled_despite_churn_flag"] is True
    # over_claim_flag also fires when claims >= 3 and consistency < 0.4
    assert sig["over_claim_flag"] is True


# ── Test 4: No-data direction — returns neutral signal ─────────────────────

@pytest.mark.asyncio
async def test_no_data_direction_returns_neutral_signal(
    db_session, project_with_directions,
):
    """Direction with no in-window tasks gets a neutral signal."""
    project = project_with_directions
    drift = await compute_direction_drift(db_session, project)
    # All three directions exist; none have tasks → neutral for each
    for did in ("d-healthy", "d-overclaim", "d-stalled"):
        sig = drift["by_direction"][did]
        assert sig["claimed_changes_count"] == 0
        assert sig["actually_merged_prs"] == 0
        assert sig["claim_shipped_consistency"] == 1.0
        assert sig["over_claim_flag"] is False
        assert sig["stalled_despite_churn_flag"] is False


# ── Test 5: Per-system rollup correctness ──────────────────────────────────

@pytest.mark.asyncio
async def test_per_system_rollup_aggregates_directions(
    db_session, project_with_directions,
):
    """sys-alpha = d-healthy (4/4) + d-overclaim (5/1) → 9 claims, 5 merged."""
    project = project_with_directions
    # d-healthy: 4 claims, 4 merged
    for _ in range(4):
        await _make_completed_task(
            db_session, project, direction_id="d-healthy",
            pr_merged=True, has_pr_artifact=True,
        )
    # d-overclaim: 5 claims, 1 merged
    await _make_completed_task(
        db_session, project, direction_id="d-overclaim",
        pr_merged=True, has_pr_artifact=True,
    )
    for _ in range(4):
        await _make_completed_task(
            db_session, project, direction_id="d-overclaim",
            pr_merged=False, has_pr_artifact=False,
        )

    drift = await compute_direction_drift(db_session, project)
    sys_alpha = drift["by_system"]["sys-alpha"]
    assert sys_alpha["claimed_changes_count"] == 9
    assert sys_alpha["actually_merged_prs"] == 5
    # 5/9 ≈ 0.556 → above 0.4 threshold so over_claim_flag NOT set system-wide.
    assert sys_alpha["over_claim_flag"] is False
    assert sys_alpha["stalled_despite_churn_flag"] is False
    # sys-beta has only d-stalled (no tasks here) → neutral signal.
    assert "sys-beta" in drift["by_system"]
    sys_beta = drift["by_system"]["sys-beta"]
    assert sys_beta["claimed_changes_count"] == 0
    assert sys_beta["over_claim_flag"] is False


# ── Test 6: Old PRs outside window are excluded ────────────────────────────

@pytest.mark.asyncio
async def test_window_excludes_older_tasks(
    db_session, project_with_directions,
):
    """Tasks older than window_days are excluded entirely."""
    project = project_with_directions
    # 3 in-window tasks (1h ago)
    for _ in range(3):
        await _make_completed_task(
            db_session, project, direction_id="d-healthy",
            pr_merged=True, has_pr_artifact=True, age_hours=1,
        )
    # 5 out-of-window tasks (10 days ago)
    for _ in range(5):
        await _make_completed_task(
            db_session, project, direction_id="d-healthy",
            pr_merged=False, has_pr_artifact=False, age_hours=240,
        )

    drift = await compute_direction_drift(db_session, project, window_days=7)
    sig = drift["by_direction"]["d-healthy"]
    assert sig["claimed_changes_count"] == 3, "out-of-window claims should be excluded"
    assert sig["actually_merged_prs"] == 3


# ── Test 7: Empty project → empty signal dicts ─────────────────────────────

@pytest.mark.asyncio
async def test_empty_project_returns_empty_dicts(db_session):
    """A project with no directions returns empty by_direction and by_system."""
    project = Project(
        name="Empty Project",
        user_id="test-user-123",
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    drift = await compute_direction_drift(db_session, project)
    assert drift["by_direction"] == {}
    assert drift["by_system"] == {}
    assert "generated_at" in drift
    assert drift["window_days"] == 7


# ── Test 8: Direction without system_id only contributes to by_direction ───

@pytest.mark.asyncio
async def test_direction_without_system_id_skips_system_rollup(db_session):
    """Directions without system_id appear in by_direction but not by_system."""
    project = Project(
        name="No-System Project",
        user_id="test-user-123",
        directions=[_direction("d-orphan")],  # no system_id
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _make_completed_task(
        db_session, project, direction_id="d-orphan",
        pr_merged=True, has_pr_artifact=True,
    )

    drift = await compute_direction_drift(db_session, project)
    assert "d-orphan" in drift["by_direction"]
    assert drift["by_system"] == {}
