"""Tests for drift_detection.compute_direction_drift, persist_drift_snapshot,
and last_n_drift_snapshots.

Synthetic claims-vs-shipped fixtures verify:
 - healthy direction: high consistency, no flags
 - over-claimer: many claims, few merges (over_claim_flag set)
 - stalled-despite-churn: claims but zero merges (stalled flag set)
 - no-data direction: returns neutral signal, not error
 - per-system rollup correctness across multiple directions
 - fresh PRs older than the window are excluded
 - persist_drift_snapshot writes rows, emits drift alerts, and prunes excess history
 - last_n_drift_snapshots returns newest-first ordered rows
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.drift_detection import (
    DirectionDriftSnapshot,
    _DRIFT_SNAPSHOT_RETENTION,
    compute_direction_drift,
    drift_history_series,
    last_n_drift_snapshots,
    persist_drift_snapshot,
)
from app.landed_signal import NON_MERGE_UNBLOCK_REASON_KEY
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)
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
        payloads=[
            {"type": "create", "prompt": "test", "current_direction_id": direction_id}
        ],
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
    db_session,
    project_with_directions,
):
    """4 claims + 4 merged PRs → consistency=1.0, no flags."""
    project = project_with_directions
    for _ in range(4):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-healthy",
            pr_merged=True,
            has_pr_artifact=True,
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
    db_session,
    project_with_directions,
):
    """5 PR-opening tasks + 1 merged → consistency=0.2, over_claim_flag set."""
    project = project_with_directions
    # 5 tasks opened PRs (have PR artifacts), only 1 actually merged
    await _make_completed_task(
        db_session,
        project,
        direction_id="d-overclaim",
        pr_merged=True,
        has_pr_artifact=True,
    )
    for _ in range(4):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-overclaim",
            pr_merged=False,
            has_pr_artifact=True,  # PR opened but not yet merged
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
    db_session,
    project_with_directions,
):
    """5 PR-opening tasks + 0 merged → stalled_despite_churn_flag set."""
    project = project_with_directions
    for _ in range(5):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-stalled",
            pr_merged=False,
            has_pr_artifact=True,  # PR opened but none merged
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
    db_session,
    project_with_directions,
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
    db_session,
    project_with_directions,
):
    """sys-alpha = d-healthy (4/4) + d-overclaim (5/1) → 9 claims, 5 merged."""
    project = project_with_directions
    # d-healthy: 4 claims, 4 merged
    for _ in range(4):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-healthy",
            pr_merged=True,
            has_pr_artifact=True,
        )
    # d-overclaim: 5 PR-opening tasks, 1 merged
    await _make_completed_task(
        db_session,
        project,
        direction_id="d-overclaim",
        pr_merged=True,
        has_pr_artifact=True,
    )
    for _ in range(4):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-overclaim",
            pr_merged=False,
            has_pr_artifact=True,  # PR opened but not merged
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
    db_session,
    project_with_directions,
):
    """Tasks older than window_days are excluded entirely."""
    project = project_with_directions
    # 3 in-window tasks (1h ago)
    for _ in range(3):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-healthy",
            pr_merged=True,
            has_pr_artifact=True,
            age_hours=1,
        )
    # 5 out-of-window tasks (10 days ago)
    for _ in range(5):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-healthy",
            pr_merged=False,
            has_pr_artifact=False,
            age_hours=240,
        )

    drift = await compute_direction_drift(db_session, project, window_days=7)
    sig = drift["by_direction"]["d-healthy"]
    assert sig["claimed_changes_count"] == 3, "out-of-window claims should be excluded"
    assert sig["actually_merged_prs"] == 3


@pytest.mark.asyncio
async def test_compute_direction_drift_counts_staging_merge_unblock_signal(
    db_session,
    project_with_directions,
):
    """A staging-rollup child (pr_merged=True + non-merge-unblock marker) opened a
    real PR that merged onto the rollup staging branch. It MUST count as a shipped
    change for drift.

    Credit-blindness fix: counting integration-landings only
    (``has_landed_signal``) missed staging-merge credit, so a healthy direction
    whose children merge to staging — before the rollup lands and re-stamps
    ``landed_on_integration`` — read as 0 merged against positive claims, producing
    false ``over_claim`` / ``stalled`` flags (wasted audit spend, poisoned
    strategist context). ``has_merge_unblock_signal`` now credits it."""
    project = project_with_directions
    task = await _make_completed_task(
        db_session,
        project,
        direction_id="d-overclaim",
        pr_merged=True,
        has_pr_artifact=True,
    )
    task.context[NON_MERGE_UNBLOCK_REASON_KEY] = "staging_rollup"
    await db_session.commit()

    drift = await compute_direction_drift(db_session, project)
    sig = drift["by_direction"]["d-overclaim"]
    assert sig["claimed_changes_count"] == 1
    assert sig["actually_merged_prs"] == 1
    # 1 claimed / 1 merged → consistent; no false over_claim flag.
    assert sig["over_claim_flag"] is False


@pytest.mark.asyncio
async def test_compute_direction_drift_excludes_confirmed_stranded_non_landing(
    db_session,
    project_with_directions,
):
    """A confirmed-stranded rollup (ancestry proved the merge never reached the
    integration branch) has its ``pr_merged`` unblock bit cleared and
    ``landed_on_integration=False`` stamped. It must NOT count as merged even though
    it opened a PR — both the landing signal and the merge-unblock signal are
    absent, so the credit-aware count still excludes genuine non-landings."""
    project = project_with_directions
    task = await _make_completed_task(
        db_session,
        project,
        direction_id="d-overclaim",
        pr_merged=False,  # stranded: unblock bit was cleared
        has_pr_artifact=True,
    )
    task.context["landed_on_integration"] = False
    task.context["rollup_landing_stranded"] = "not_ancestor"
    await db_session.commit()

    drift = await compute_direction_drift(db_session, project)
    sig = drift["by_direction"]["d-overclaim"]
    assert sig["claimed_changes_count"] == 1
    assert sig["actually_merged_prs"] == 0


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
        db_session,
        project,
        direction_id="d-orphan",
        pr_merged=True,
        has_pr_artifact=True,
    )

    drift = await compute_direction_drift(db_session, project)
    assert "d-orphan" in drift["by_direction"]
    assert drift["by_system"] == {}


# ── Test 9: persist_drift_snapshot writes one row per direction ────────────


@pytest.mark.asyncio
async def test_persist_drift_snapshot_writes_rows(db_session):
    """persist_drift_snapshot inserts one DirectionDriftSnapshot per direction."""
    project = Project(
        name="Snapshot Write Project",
        user_id="test-user-123",
        directions=[_direction("d-a"), _direction("d-b")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    drift = await compute_direction_drift(db_session, project)
    await persist_drift_snapshot(db_session, project, drift)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DirectionDriftSnapshot).where(
                    DirectionDriftSnapshot.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    direction_ids = {r.direction_id for r in rows}
    assert direction_ids == {"d-a", "d-b"}
    for row in rows:
        assert row.claim_shipped_consistency == 1.0
        assert row.over_claim_flag is False
        assert row.stalled_despite_churn_flag is False


# ── Test 10: persist_drift_snapshot prunes to retention limit ─────────────


@pytest.mark.asyncio
async def test_persist_drift_snapshot_prunes_excess(db_session):
    """After _DRIFT_SNAPSHOT_RETENTION + 1 writes, only the newest rows remain."""
    project = Project(
        name="Snapshot Prune Project",
        user_id="test-user-123",
        directions=[_direction("d-prune")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    neutral_drift = {
        "by_direction": {
            "d-prune": {
                "claimed_changes_count": 0,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 1.0,
                "over_claim_flag": False,
                "stalled_despite_churn_flag": False,
            }
        }
    }

    # Insert _DRIFT_SNAPSHOT_RETENTION + 1 snapshots (one over the limit).
    for _ in range(_DRIFT_SNAPSHOT_RETENTION + 1):
        await persist_drift_snapshot(db_session, project, neutral_drift)
        await db_session.commit()

    count_result = await db_session.execute(
        select(DirectionDriftSnapshot).where(
            DirectionDriftSnapshot.project_id == project.id,
            DirectionDriftSnapshot.direction_id == "d-prune",
        )
    )
    rows = count_result.scalars().all()
    assert len(rows) == _DRIFT_SNAPSHOT_RETENTION, (
        f"Expected {_DRIFT_SNAPSHOT_RETENTION} rows after pruning, got {len(rows)}"
    )


# ── Test 11: persist_drift_snapshot is a no-op for empty drift ────────────


@pytest.mark.asyncio
async def test_persist_drift_snapshot_noop_on_empty(db_session):
    """persist_drift_snapshot does nothing when by_direction is empty."""
    project = Project(
        name="Empty Snapshot Project",
        user_id="test-user-123",
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    empty_drift = {"by_direction": {}}
    await persist_drift_snapshot(db_session, project, empty_drift)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DirectionDriftSnapshot).where(
                    DirectionDriftSnapshot.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# ── Test 12: last_n_drift_snapshots returns newest-first ─────────────────


@pytest.mark.asyncio
async def test_last_n_drift_snapshots_returns_newest_first(db_session):
    """last_n_drift_snapshots returns rows ordered newest-first, limited to n."""
    project = Project(
        name="Snapshot Query Project",
        user_id="test-user-123",
        directions=[_direction("d-query")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    drift_with_flags = {
        "by_direction": {
            "d-query": {
                "claimed_changes_count": 5,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 0.0,
                "over_claim_flag": True,
                "stalled_despite_churn_flag": True,
            }
        }
    }
    neutral_drift = {
        "by_direction": {
            "d-query": {
                "claimed_changes_count": 0,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 1.0,
                "over_claim_flag": False,
                "stalled_despite_churn_flag": False,
            }
        }
    }

    # Write 3 neutral then 1 flagged (newest).
    for _ in range(3):
        await persist_drift_snapshot(db_session, project, neutral_drift)
        await db_session.commit()
    await persist_drift_snapshot(db_session, project, drift_with_flags)
    await db_session.commit()

    snapshots = await last_n_drift_snapshots(db_session, project.id, "d-query", n=2)
    assert len(snapshots) == 2
    # Newest should have the flags set.
    assert snapshots[0].over_claim_flag is True
    assert snapshots[0].stalled_despite_churn_flag is True
    # Second-newest was neutral.
    assert snapshots[1].over_claim_flag is False


# ── Test 13: Non-default thresholds from runtime_config are respected ───────


@pytest.mark.asyncio
async def test_custom_thresholds_from_runtime_config_are_respected(db_session):
    """Lowered over_claim_min_claims=2 should flag a direction with 2 claims, 0 merges."""
    project = Project(
        name="Custom Threshold Project",
        user_id="test-user-123",
        directions=[_direction("d-threshold")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 tasks opened PRs, none merged — won't flag with default min=3, will with min=2
    for _ in range(2):
        await _make_completed_task(
            db_session,
            project,
            direction_id="d-threshold",
            pr_merged=False,
            has_pr_artifact=True,  # PR opened but not merged
        )

    patched_config = {
        "drift_over_claim_min_claims": 2,
        "drift_over_claim_consistency_threshold": 0.5,
        "drift_stall_min_claims": 2,
    }
    with patch("app.shared_state.runtime_config", patched_config):
        drift = await compute_direction_drift(db_session, project)

    sig = drift["by_direction"]["d-threshold"]
    assert sig["claimed_changes_count"] == 2
    assert sig["over_claim_flag"] is True, (
        "lowered threshold should trigger over_claim_flag"
    )
    assert sig["stalled_despite_churn_flag"] is True, (
        "lowered stall_min should trigger stalled flag"
    )


# ── Test 14: drift_history_series returns serialised sparkline data ─────────


@pytest.mark.asyncio
async def test_drift_history_series_returns_serialised_sparkline_data(db_session):
    """drift_history_series returns newest-first serialised dicts with ISO timestamps."""
    project = Project(
        name="History Series Project",
        user_id="test-user-123",
        directions=[_direction("d-series")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    flagged_drift = {
        "by_direction": {
            "d-series": {
                "claimed_changes_count": 4,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 0.0,
                "over_claim_flag": True,
                "stalled_despite_churn_flag": True,
            }
        }
    }
    neutral_drift = {
        "by_direction": {
            "d-series": {
                "claimed_changes_count": 0,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 1.0,
                "over_claim_flag": False,
                "stalled_despite_churn_flag": False,
            }
        }
    }

    # 3 neutral snapshots then 1 flagged (newest).
    for _ in range(3):
        await persist_drift_snapshot(db_session, project, neutral_drift)
        await db_session.commit()
    await persist_drift_snapshot(db_session, project, flagged_drift)
    await db_session.commit()

    series = await drift_history_series(db_session, project.id, "d-series", n=3)
    assert len(series) == 3
    # Newest first: most recent was flagged.
    assert series[0]["over_claim_flag"] is True
    assert series[0]["claim_shipped_consistency"] == 0.0
    assert isinstance(series[0]["snapshot_at"], str), "snapshot_at must be ISO string"
    # Second-newest was neutral.
    assert series[1]["over_claim_flag"] is False
    assert series[1]["claim_shipped_consistency"] == 1.0
    # All entries must have the expected keys.
    for entry in series:
        assert "snapshot_at" in entry
        assert "claim_shipped_consistency" in entry
        assert "over_claim_flag" in entry
        assert "stalled_despite_churn_flag" in entry


# ── Tests for Alert creation on drift flags ────────────────────────────────


@pytest.mark.asyncio
async def test_no_drift_creates_no_alert(db_session):
    """persist_drift_snapshot with no flags creates no Alert rows."""
    project = Project(
        name="No Drift Alerts Project",
        user_id="test-user-123",
        directions=[_direction("d-clean")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    neutral = {
        "by_direction": {
            "d-clean": {
                "claimed_changes_count": 0,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 1.0,
                "over_claim_flag": False,
                "stalled_despite_churn_flag": False,
            }
        }
    }
    await persist_drift_snapshot(db_session, project, neutral)
    await db_session.commit()

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_over_claim_flag_creates_warning_alert(db_session):
    """A single over_claim_flag snapshot creates one WARNING Alert."""
    project = Project(
        name="Over Claim Alert Project",
        user_id="test-user-123",
        directions=[_direction("d-oc")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    flagged = {
        "by_direction": {
            "d-oc": {
                "claimed_changes_count": 5,
                "actually_merged_prs": 1,
                "claim_shipped_consistency": 0.2,
                "over_claim_flag": True,
                "stalled_despite_churn_flag": False,
            }
        }
    }
    await persist_drift_snapshot(db_session, project, flagged)
    await db_session.commit()

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "over-claim drift" in alerts[0].title
    assert "d-oc" in alerts[0].title
    assert "5" in alerts[0].message
    assert "1" in alerts[0].message


@pytest.mark.asyncio
async def test_repeated_drift_flag_no_duplicate_alert(db_session):
    """Persisting the same flagged signal twice yields only one Alert (dedup)."""
    project = Project(
        name="Dedup Alert Project",
        user_id="test-user-123",
        directions=[_direction("d-dup")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    flagged = {
        "by_direction": {
            "d-dup": {
                "claimed_changes_count": 4,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 0.0,
                "over_claim_flag": True,
                "stalled_despite_churn_flag": False,
            }
        }
    }
    await persist_drift_snapshot(db_session, project, flagged)
    await db_session.commit()
    await persist_drift_snapshot(db_session, project, flagged)
    await db_session.commit()

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "second persist should not create a duplicate alert"


@pytest.mark.asyncio
async def test_flag_cleared_no_new_alert(db_session):
    """After a flag fires once, a subsequent neutral snapshot creates no new Alert."""
    project = Project(
        name="Flag Cleared Project",
        user_id="test-user-123",
        directions=[_direction("d-clear")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    flagged = {
        "by_direction": {
            "d-clear": {
                "claimed_changes_count": 3,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 0.0,
                "over_claim_flag": True,
                "stalled_despite_churn_flag": False,
            }
        }
    }
    neutral = {
        "by_direction": {
            "d-clear": {
                "claimed_changes_count": 0,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 1.0,
                "over_claim_flag": False,
                "stalled_despite_churn_flag": False,
            }
        }
    }
    await persist_drift_snapshot(db_session, project, flagged)
    await db_session.commit()
    await persist_drift_snapshot(db_session, project, neutral)
    await db_session.commit()

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "neutral snapshot should not create additional alerts"


@pytest.mark.asyncio
async def test_stalled_flag_creates_warning_alert(db_session):
    """A stalled_despite_churn_flag snapshot creates a stalled-despite-churn Alert."""
    project = Project(
        name="Stalled Alert Project",
        user_id="test-user-123",
        directions=[_direction("d-stall-alert")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Use patched thresholds so only stalled fires (over_claim_min raised above claimed)
    flagged = {
        "by_direction": {
            "d-stall-alert": {
                "claimed_changes_count": 5,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 0.0,
                "over_claim_flag": False,
                "stalled_despite_churn_flag": True,
            }
        }
    }
    await persist_drift_snapshot(db_session, project, flagged)
    await db_session.commit()

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "stalled-despite-churn drift" in alerts[0].title
    assert "d-stall-alert" in alerts[0].title


@pytest.mark.asyncio
async def test_both_flags_create_two_alerts(db_session):
    """When both over_claim_flag and stalled_despite_churn_flag are set, two Alerts are created."""
    project = Project(
        name="Both Flags Project",
        user_id="test-user-123",
        directions=[_direction("d-both")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    flagged = {
        "by_direction": {
            "d-both": {
                "claimed_changes_count": 5,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 0.0,
                "over_claim_flag": True,
                "stalled_despite_churn_flag": True,
            }
        }
    }
    await persist_drift_snapshot(db_session, project, flagged)
    await db_session.commit()

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 2
    titles = {a.title for a in alerts}
    assert any("over-claim drift" in t for t in titles)
    assert any("stalled-despite-churn drift" in t for t in titles)


@pytest.mark.asyncio
async def test_drift_history_series_returns_empty_for_no_history(db_session):
    """drift_history_series returns [] when no snapshots exist yet."""
    project = Project(
        name="Empty History Project",
        user_id="test-user-123",
        directions=[_direction("d-empty-series")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    series = await drift_history_series(db_session, project.id, "d-empty-series")
    assert series == []


# ── Test: PR lifecycle cluster counts as one claim ────────────────────────────


@pytest.mark.asyncio
async def test_pr_lifecycle_cluster_counts_as_one_claim(
    db_session,
    project_with_directions,
):
    """A full lifecycle cluster (create+fix+review) for one PR counts as one claim.

    The create task opens a PR (has a PULL_REQUEST artifact).  The fix and
    review child tasks share the same direction_id but never open a separate
    PR artifact — they are lifecycle children of the same code-change.  Only
    the PR-opening task should count toward claimed_changes_count; fix/review
    tasks without their own artifact must not inflate the claim count.
    """
    project = project_with_directions

    # Create task opens the PR — has a PR artifact.
    await _make_completed_task(
        db_session,
        project,
        direction_id="d-healthy",
        pr_merged=True,
        has_pr_artifact=True,
    )
    # Fix task: lifecycle child of the same PR; no separate PR artifact.
    await _make_completed_task(
        db_session,
        project,
        direction_id="d-healthy",
        pr_merged=False,
        has_pr_artifact=False,
        plan_output={"summary": "Applied fix to PR"},
    )
    # Review task: another lifecycle child; no separate PR artifact.
    await _make_completed_task(
        db_session,
        project,
        direction_id="d-healthy",
        pr_merged=False,
        has_pr_artifact=False,
        plan_output={"findings": ["looks good"]},
    )

    drift = await compute_direction_drift(db_session, project)
    sig = drift["by_direction"]["d-healthy"]
    # Only the create task (with PR artifact) counts as a claim; fix and review
    # tasks without their own artifact must NOT inflate the count.
    assert sig["claimed_changes_count"] == 1, (
        "only the PR-opening task should count; fix/review children without "
        "a PR artifact must not contribute to claimed_changes_count"
    )
    assert sig["actually_merged_prs"] == 1
    assert sig["claim_shipped_consistency"] == 1.0
    assert sig["over_claim_flag"] is False
    assert sig["stalled_despite_churn_flag"] is False


# ── Test: completed task without PR artifact does not count as claim ──────────


@pytest.mark.asyncio
async def test_completed_task_without_pr_artifact_does_not_count_as_claim(
    db_session,
    project_with_directions,
):
    """A COMPLETED task with plan_output but no PR artifact contributes 0 claims."""
    project = project_with_directions

    # Task is COMPLETED and has a rich plan_output — but no PR artifact.
    await _make_completed_task(
        db_session,
        project,
        direction_id="d-healthy",
        pr_merged=False,
        has_pr_artifact=False,
        plan_output={"summary": "Research complete", "findings": ["f1", "f2"]},
    )

    drift = await compute_direction_drift(db_session, project)
    sig = drift["by_direction"]["d-healthy"]
    assert sig["claimed_changes_count"] == 0, (
        "a completed task without a PULL_REQUEST artifact must not count as a claim"
    )
    assert sig["actually_merged_prs"] == 0
    assert sig["claim_shipped_consistency"] == 1.0
