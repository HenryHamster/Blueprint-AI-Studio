"""Focused M5 rollout-health and seven-day-soak readiness tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Project,
    ProjectMember,
    ProjectMessage,
    StrategicCycle,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.strategic_resident_checkpointing import initialize_strategic_resident
from app.strategic_resident_legacy_projection import (
    LEGACY_CONTINUATION_KEY,
    LEGACY_SESSION_KEY,
)
from app.strategic_resident_rollout_health import (
    RESIDENT_SHADOW_PROOF_MAX_AGE,
    RESIDENT_SOAK_MINIMUM,
    _task_projection,
    build_resident_rollout_health,
)
from app.strategic_resident_turns import (
    STRATEGIC_RESIDENT_TURN_TASK_TYPE,
    coalesce_shadow_resident_turn,
)
from app.time_utils import _utcnow
from testing._resident_seed import seed_without_authority_enforcement


async def _resident_project(db_session):
    project = Project(name="Resident rollout health", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    resident = await initialize_strategic_resident(
        db_session, project.id, locked_project=project
    )
    await db_session.commit()
    return project, resident


async def _record_shadow_proof(
    db_session,
    project: Project,
    resident: StrategicResident,
    *,
    handled_sequence: int = 0,
    completed_at: datetime | None = None,
    mismatch_reasons: list[str] | None = None,
) -> Task:
    completed_at = completed_at or _utcnow()
    checkpoint = await db_session.get(
        StrategicResidentCheckpoint, resident.current_checkpoint_id
    )
    assert checkpoint is not None
    task = Task(
        project_id=project.id,
        title="Resident shadow proof",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": STRATEGIC_RESIDENT_TURN_TASK_TYPE, "shadow": True}],
        plan_output={
            "resident_shadow": {
                "manifest": {
                    "checkpoint": {
                        "version": checkpoint.version,
                        "content_hash": checkpoint.content_hash,
                    }
                }
            }
        },
        completed_at=completed_at,
        updated_at=completed_at,
    )
    db_session.add(task)
    await db_session.flush()
    message_range = [1, handled_sequence] if handled_sequence else None
    resident.scheduling_state = {
        "shadow_projection_state": "healthy",
        "shadow_handled_message_sequence": handled_sequence,
        "shadow_last_completed_task_id": str(task.id),
        "shadow_last_completed_at": completed_at.isoformat(),
        "shadow_last_comparison": {
            "checkpoint_lineage_matches": True,
            "requested_message_range": message_range,
            "represented_message_range": message_range,
            "requested_chip_count": 0,
            "represented_chip_count": 0,
            "shadow_scheduling_decision": "completed_and_promote",
            "mismatch_reasons": mismatch_reasons or [],
        },
    }
    db_session.add(resident)
    await db_session.commit()
    return task


@pytest.mark.asyncio
async def test_health_reports_uninitialized_project_without_side_effects(db_session):
    project = Project(name="No resident", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()

    result = await build_resident_rollout_health(db_session, project.id)

    assert result["resident_initialized"] is False
    assert result["cutover_ready"] is False
    assert result["blockers"] == ["resident_not_initialized"]
    assert await db_session.get(StrategicResident, project.id) is None


@pytest.mark.asyncio
async def test_owner_health_endpoint_is_bounded_and_role_gated(client, db_session):
    project = Project(name="Health endpoint", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    membership = ProjectMember(
        project_id=project.id,
        user_id="test-user-123",
        role="owner",
    )
    db_session.add(membership)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/resident-engine/health"
    )

    assert response.status_code == 200
    assert response.json()["blockers"] == ["resident_not_initialized"]

    membership.role = "member"
    db_session.add(membership)
    await db_session.commit()
    forbidden = await client.get(
        f"/api/admin/projects/{project.id}/resident-engine/health"
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_shadow_uses_all_user_high_water_and_requires_current_proof(db_session):
    project, resident = await _resident_project(db_session)
    # A *handled* user conversation message whose sequence sits beyond the
    # shadow's covered high-water (7 vs. handled 6). On Postgres the 0608 BEFORE
    # INSERT trigger both re-allocates the sequence (ignoring the literal 7) and
    # forces a fresh user message to handling_state='pending'; SQLite has neither
    # trigger and keeps the authored values. Seed through
    # ``seed_without_authority_enforcement`` so this setup-only starting state
    # lands as authored on both dialects. The assertion under test — shadow
    # backlog detection on an already-handled conversation — runs afterwards with
    # enforcement restored.
    async with seed_without_authority_enforcement(db_session):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                sequence=7,
                surface="conversation",
                handling_state="responded",
                from_type="user",
                content="Please decide this.",
            )
        )
        await db_session.commit()
    await _record_shadow_proof(
        db_session, project, resident, handled_sequence=6
    )

    blocked = await build_resident_rollout_health(db_session, project.id)
    assert blocked["cutover_ready"] is False
    assert "shadow_message_backlog" in blocked["cutover_blockers"]
    assert blocked["conversation"]["latest_unhandled_user_sequence"] is None

    resident.scheduling_state = {
        **resident.scheduling_state,
        "shadow_handled_message_sequence": 7,
    }
    db_session.add(resident)
    await db_session.commit()
    ready = await build_resident_rollout_health(db_session, project.id)
    assert ready["cutover_ready"] is True
    assert ready["cutover_blockers"] == []
    assert ready["rollback_blockers"] == ["not_cut_over"]


@pytest.mark.asyncio
async def test_missing_and_expired_shadow_proofs_fail_closed(db_session):
    project, resident = await _resident_project(db_session)
    missing = await build_resident_rollout_health(db_session, project.id)
    assert "shadow_comparison_missing_or_stale" in missing["cutover_blockers"]
    assert "shadow_proof_expired" in missing["cutover_blockers"]

    stale_at = _utcnow() - RESIDENT_SHADOW_PROOF_MAX_AGE - timedelta(seconds=1)
    await _record_shadow_proof(
        db_session, project, resident, completed_at=stale_at
    )
    expired = await build_resident_rollout_health(db_session, project.id)
    assert expired["shadow"]["checkpoint_current"] is True
    assert expired["shadow"]["proof_fresh"] is False
    assert "shadow_proof_expired" in expired["cutover_blockers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("chip_count", [None, -1])
async def test_shadow_comparison_requires_explicit_nonnegative_chip_counts(
    db_session, chip_count
):
    project, resident = await _resident_project(db_session)
    await _record_shadow_proof(db_session, project, resident)
    comparison = dict(resident.scheduling_state["shadow_last_comparison"])
    if chip_count is None:
        comparison.pop("requested_chip_count")
        comparison.pop("represented_chip_count")
    else:
        comparison["requested_chip_count"] = chip_count
        comparison["represented_chip_count"] = chip_count
    resident.scheduling_state = {
        **resident.scheduling_state,
        "shadow_last_comparison": comparison,
    }
    db_session.add(resident)
    await db_session.commit()

    result = await build_resident_rollout_health(db_session, project.id)

    assert result["cutover_ready"] is False
    assert "shadow_comparison_missing_or_stale" in result["cutover_blockers"]
    assert result["shadow"]["last_comparison"]["requested_chip_count"] is None
    assert result["shadow"]["last_comparison"]["represented_chip_count"] is None


@pytest.mark.asyncio
async def test_readiness_reuses_transition_pointer_lineage_preflight(db_session):
    project, resident = await _resident_project(db_session)
    await _record_shadow_proof(db_session, project, resident)
    wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="interactive",
        trigger_kind="operator_message",
        message_sequence=1,
    )
    await db_session.commit()
    assert wake.task_id is not None

    valid = await build_resident_rollout_health(db_session, project.id)
    assert "pointer_lineage_invalid" not in valid["cutover_blockers"]

    active = await db_session.get(Task, wake.task_id)
    assert active is not None
    active.context = {**(active.context or {}), "_resident_lane": "background"}
    db_session.add(active)
    await db_session.commit()

    cutover = await build_resident_rollout_health(db_session, project.id)
    assert "pointer_lineage_invalid" in cutover["cutover_blockers"]
    assert cutover["cutover_ready"] is False

    resident.engine_owner = "strategic_resident"
    # Ownership flips must advance the authority generation (Postgres
    # trg_strategicresident_authority_monotonic); SQLite lacks the trigger.
    resident.authority_generation += 1
    db_session.add(resident)
    await db_session.commit()
    rollback = await build_resident_rollout_health(db_session, project.id)
    assert "pointer_lineage_invalid" in rollback["rollback_blockers"]
    assert rollback["rollback_ready"] is False


@pytest.mark.asyncio
async def test_legacy_session_cycle_and_task_preflight_match_transition(db_session):
    project, resident = await _resident_project(db_session)
    await _record_shadow_proof(db_session, project, resident)
    project.vision = {
        LEGACY_CONTINUATION_KEY: {"cursor": "pending"},
        LEGACY_SESSION_KEY: {
            "status": "running",
            "stop_requested": False,
            "task_id": "not-a-uuid",
        },
    }
    db_session.add(project)
    db_session.add(
        StrategicCycle(
            id=uuid.uuid4().hex,
            project_id=project.id,
            outcome="phase1_running",
        )
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="Legacy Resident turn",
            status=TaskStatus.IN_PROGRESS,
            context={"_resident_session": True},
        )
    )
    await db_session.commit()

    result = await build_resident_rollout_health(db_session, project.id)
    assert {
        "legacy_continuation_active",
        "legacy_session_not_drained",
        "legacy_cycle_active",
        "legacy_task_active",
        "legacy_session_integrity_invalid",
    }.issubset(result["cutover_blockers"])
    assert result["legacy_preflight"]["active_cycle_count"] == 1
    assert result["legacy_preflight"]["active_task_count"] == 1
    assert result["legacy_preflight"]["session_integrity_valid"] is False


@pytest.mark.asyncio
async def test_retirement_requires_healthy_soak_and_rollback_rehearsal(db_session):
    project, resident = await _resident_project(db_session)
    now = _utcnow()
    resident.engine_owner = "strategic_resident"
    # Ownership flips must advance the authority generation (Postgres
    # trg_strategicresident_authority_monotonic); SQLite lacks the trigger.
    resident.authority_generation += 1
    resident.health_status = "healthy"
    resident.last_healthy_at = now
    resident.rollout_state = {
        "cutover_completed_at": (
            now - RESIDENT_SOAK_MINIMUM - timedelta(minutes=1)
        ).isoformat(),
        "rollback_completed_at": (now - timedelta(days=8)).isoformat(),
    }
    db_session.add(resident)
    await db_session.commit()

    result = await build_resident_rollout_health(db_session, project.id, now=now)

    assert result["rollback_ready"] is True
    assert result["legacy_retirement_ready"] is True
    assert result["retirement_blockers"] == []
    assert result["soak"]["observed_seconds"] >= 7 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_retirement_fails_closed_before_real_seven_day_window(db_session):
    project, resident = await _resident_project(db_session)
    now = _utcnow()
    resident.engine_owner = "strategic_resident"
    # Ownership flips must advance the authority generation (Postgres
    # trg_strategicresident_authority_monotonic); SQLite lacks the trigger.
    resident.authority_generation += 1
    resident.health_status = "healthy"
    resident.last_healthy_at = now
    resident.rollout_state = {
        "cutover_completed_at": (now - timedelta(days=6, hours=23)).isoformat(),
        "rollback_completed_at": (now - timedelta(days=8)).isoformat(),
    }
    db_session.add(resident)
    await db_session.commit()

    result = await build_resident_rollout_health(db_session, project.id, now=now)

    assert result["legacy_retirement_ready"] is False
    assert "seven_day_soak_incomplete" in result["retirement_blockers"]


@pytest.mark.asyncio
async def test_checkpoint_hash_integrity_and_bounded_shadow_projection(db_session):
    project, resident = await _resident_project(db_session)
    oversized = [f"mismatch-{index}-" + ("x" * 500) for index in range(100)]
    await _record_shadow_proof(
        db_session, project, resident, mismatch_reasons=oversized
    )

    bounded = await build_resident_rollout_health(db_session, project.id)
    comparison = bounded["shadow"]["last_comparison"]
    assert len(comparison["mismatch_reasons"]) == 20
    assert all(len(reason) <= 160 for reason in comparison["mismatch_reasons"])
    assert comparison["truncated"] is True
    assert "shadow_comparison_missing_or_stale" in bounded["cutover_blockers"]

    checkpoint = await db_session.get(
        StrategicResidentCheckpoint, resident.current_checkpoint_id
    )
    # Checkpoints are immutable in production (0611
    # trg_strategicresidentcheckpoint_immutability), so forcing a corrupted
    # content_hash to exercise the integrity check is a setup-only mutation the
    # schema legitimately rejects. Relax enforcement for that single write; the
    # integrity assertions below run with the trigger restored. SQLite has no
    # such trigger, so this is a no-op there.
    async with seed_without_authority_enforcement(db_session):
        checkpoint.content_hash = "0" * 64
        db_session.add(checkpoint)
        await db_session.commit()
    tampered = await build_resident_rollout_health(db_session, project.id)
    assert tampered["checkpoint"]["integrity_valid"] is False
    assert "checkpoint_integrity_invalid" in tampered["cutover_blockers"]


@pytest.mark.asyncio
async def test_utc_normalization_does_not_overstate_seven_day_soak(db_session):
    project, resident = await _resident_project(db_session)
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    resident.engine_owner = "strategic_resident"
    # Ownership flips must advance the authority generation (Postgres
    # trg_strategicresident_authority_monotonic); SQLite lacks the trigger.
    resident.authority_generation += 1
    resident.health_status = "healthy"
    resident.last_healthy_at = now.replace(tzinfo=None)
    resident.rollout_state = {
        "cutover_completed_at": (now - timedelta(days=6, hours=23)).isoformat(),
        "rollback_completed_at": (now - timedelta(days=8)).isoformat(),
    }
    db_session.add(resident)
    await db_session.commit()

    result = await build_resident_rollout_health(db_session, project.id, now=now)

    assert result["observed_at"].endswith("Z")
    assert result["soak"]["observed_seconds"] == 6 * 86400 + 23 * 3600
    assert "seven_day_soak_incomplete" in result["retirement_blockers"]


@pytest.mark.asyncio
async def test_malformed_rehearsal_and_soak_failures_are_visible(db_session):
    project, resident = await _resident_project(db_session)
    now = _utcnow()
    cutover_at = now - timedelta(days=8)
    resident.engine_owner = "strategic_resident"
    # Ownership flips must advance the authority generation (Postgres
    # trg_strategicresident_authority_monotonic); SQLite lacks the trigger.
    resident.authority_generation += 1
    resident.health_status = "healthy"
    resident.last_healthy_at = now
    resident.last_error_at = now - timedelta(hours=2)
    resident.rollout_state = {
        "cutover_completed_at": cutover_at.isoformat(),
        "rollback_completed_at": "not-a-timestamp",
    }
    failed = Task(
        project_id=project.id,
        title="Failed Resident turn",
        status=TaskStatus.FAILED,
        payloads=[{"type": STRATEGIC_RESIDENT_TURN_TASK_TYPE}],
        updated_at=now - timedelta(hours=1),
    )
    db_session.add_all([resident, failed])
    await db_session.commit()

    result = await build_resident_rollout_health(db_session, project.id, now=now)

    assert result["soak"]["error_free"] is False
    assert result["soak"]["failed_turn_count"] == 1
    assert {
        "rollback_rehearsal_missing_or_invalid",
        "resident_error_during_soak",
        "failed_resident_turn_during_soak",
    }.issubset(result["retirement_blockers"])


def test_task_status_projection_is_canonical():
    task = Task(project_id=uuid.uuid4(), status=TaskStatus.IN_PROGRESS)
    projection = _task_projection(task)
    assert projection is not None
    assert projection["status"] == TaskStatus.IN_PROGRESS.value
