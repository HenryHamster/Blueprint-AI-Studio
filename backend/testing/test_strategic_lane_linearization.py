"""PostgreSQL proofs for the automatic Strategic lane authority boundary."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.auth import create_agent_token
from app.branch_resolution import backfill_task_branch_namespace
from app.build_config_detection import (
    maybe_refresh_empty_build_config,
    persist_discovered_build_commands,
)
from app.landed_signal import corroborate_and_apply_merged_landed_signal
from app.models import Project, ProjectMember, Task, TaskStatus
from app.repo_bootstrap import (
    BOOTSTRAP_RECOVERY_MAX_ATTEMPTS,
    spawn_bootstrap_task,
)
from app.strategic_spawning import _finalize_strategic_review_spawn


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _strategic_finalize_kwargs(project_id: uuid.UUID) -> dict:
    return {
        "project_id": project_id,
        "project_name": "linearized strategic lane",
        "payload": {"type": "strategic_review"},
        "cycle_id": str(uuid.uuid4()),
        "strategy_only": True,
        "base_vision": {},
        "base_problem_model": {},
        "base_running_update": "",
        "local_runner_intent": {},
        "preferred_runner_target_id": None,
        "extra_context": None,
        "operator_steered": False,
        "steering_excerpt": None,
    }


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_lane_disable_lock_wins_automatic_strategic_insert_race(db_session):
    """A concurrent false write cannot leave a latent Strategic task behind."""

    project = Project(
        name="strategic-lane-disable-race",
        user_id="test-user-123",
        build_config={"automatic_strategic_cycles_enabled": True},
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    disable_holds_lock = asyncio.Event()
    spawn_checked = asyncio.Event()
    suppression_reason: dict[str, str] = {}

    async def _disable_lane() -> None:
        async with Session() as session:
            locked = (
                await session.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            locked.build_config = {"automatic_strategic_cycles_enabled": False}
            session.add(locked)
            await session.flush()
            disable_holds_lock.set()
            await asyncio.wait_for(spawn_checked.wait(), timeout=5)
            await session.commit()

    async def _automatic_spawn() -> bool:
        await asyncio.wait_for(disable_holds_lock.wait(), timeout=5)
        async with Session() as session:
            try:
                return await _finalize_strategic_review_spawn(
                    session,
                    **_strategic_finalize_kwargs(project_id),
                    automatic_creation=True,
                    suppression_reason_out=suppression_reason,
                )
            finally:
                spawn_checked.set()

    _, spawned = await asyncio.wait_for(
        asyncio.gather(_disable_lane(), _automatic_spawn()),
        timeout=10,
    )

    assert spawned is False
    assert suppression_reason == {"reason": "project_unavailable"}
    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    strategic_task = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.payloads[0]["type"].as_string() == "strategic_review",
            )
        )
    ).scalar_one_or_none()
    assert strategic_task is None


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_finalizer_reports_lane_disabled_suppression(db_session):
    """The boolean spawn API also exposes its exact authority suppression."""

    project = Project(
        name="strategic-lane-disabled-reason",
        user_id="test-user-123",
        build_config={"automatic_strategic_cycles_enabled": False},
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id
    suppression_reason: dict[str, str] = {}

    spawned = await _finalize_strategic_review_spawn(
        db_session,
        **_strategic_finalize_kwargs(project_id),
        automatic_creation=True,
        suppression_reason_out=suppression_reason,
    )

    assert spawned is False
    assert suppression_reason == {"reason": "automatic_strategic_cycles_disabled"}
    task = (
        await db_session.execute(select(Task).where(Task.project_id == project_id))
    ).scalar_one_or_none()
    assert task is None


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_patch_false_wins_inflight_redetection_merge(
    client: AsyncClient,
    db_session,
):
    """A detector started from stale config re-merges after PATCH commits."""

    project = Project(
        name="build-config-redetect-race",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        build_config={
            "repo_is_empty": True,
            "source_file_count": 0,
            "automatic_strategic_cycles_enabled": True,
        },
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    detector_started = asyncio.Event()
    release_detector = asyncio.Event()

    async def _delayed_detection(*_args, **_kwargs):
        detector_started.set()
        await asyncio.wait_for(release_detector.wait(), timeout=5)
        return {
            "directories": [{"path": ".", "type": "python", "test_cmd": "pytest"}],
            "repo_is_empty": False,
            "source_file_count": 8,
            "has_ci": True,
        }

    async def _redetect_from_stale_snapshot() -> bool:
        async with Session() as session:
            stale_project = await session.get(Project, project_id)
            assert stale_project is not None
            return await maybe_refresh_empty_build_config(stale_project, session)

    with (
        patch(
            "app.github.get_repo_installation_token",
            new=AsyncMock(return_value="token"),
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new=AsyncMock(side_effect=_delayed_detection),
        ),
    ):
        redetect_task = asyncio.create_task(_redetect_from_stale_snapshot())
        await asyncio.wait_for(detector_started.wait(), timeout=5)

        patch_response = await client.patch(
            f"/api/admin/projects/{project_id}",
            json={"build_config": {"automatic_strategic_cycles_enabled": False}},
        )
        assert patch_response.status_code == 200, patch_response.text
        assert (
            patch_response.json()["build_config"]["automatic_strategic_cycles_enabled"]
            is False
        )

        release_detector.set()
        assert await asyncio.wait_for(redetect_task, timeout=10) is True

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed.build_config["repo_is_empty"] is False
    assert refreshed.build_config["source_file_count"] == 8


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_patch_false_wins_inflight_agent_command_writeback(
    client: AsyncClient,
    db_session,
):
    """A task command writeback re-merges from the locked current config."""

    project = Project(
        name="agent-command-writeback-race",
        user_id="test-user-123",
        build_config={
            "automatic_strategic_cycles_enabled": True,
            "directories": [{"path": ".", "type": "python", "test_cmd": "pytest"}],
        },
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    task = Task(
        project_id=project.id,
        payloads=[{"type": "repo_bootstrap"}],
        vm_instance_id="vm-command-writeback-race",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    project_id = project.id
    task_id = task.id

    from app.task_locking import lock_project_or_429 as real_project_lock

    writer_reached_final_seam = asyncio.Event()
    release_writer = asyncio.Event()

    async def _delayed_final_lock(*args, **kwargs):
        # The endpoint has already loaded its advisory Project snapshot before
        # importing/calling this final authority helper. Pause here so the
        # operator PATCH deterministically commits between stale load and merge.
        writer_reached_final_seam.set()
        await asyncio.wait_for(release_writer.wait(), timeout=5)
        return await real_project_lock(*args, **kwargs)

    token = create_agent_token(str(task_id), "vm-command-writeback-race")
    with patch(
        "app.task_locking.lock_project_or_429",
        new=_delayed_final_lock,
    ):
        writeback_task = asyncio.create_task(
            client.patch(
                f"/api/agent/projects/{project_id}/build-config/commands",
                json={
                    "task_id": str(task_id),
                    "entries": [{"path": ".", "confirmed_test_cmd": "pytest -x"}],
                },
                headers={"x-agent-token": token},
            )
        )
        await asyncio.wait_for(writer_reached_final_seam.wait(), timeout=5)

        patch_response = await client.patch(
            f"/api/admin/projects/{project_id}",
            json={"build_config": {"automatic_strategic_cycles_enabled": False}},
        )
        assert patch_response.status_code == 200, patch_response.text

        release_writer.set()
        writeback_response = await asyncio.wait_for(writeback_task, timeout=10)
        assert writeback_response.status_code == 200, writeback_response.text

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed.build_config["directories"][0]["confirmed_test_cmd"] == (
        "pytest -x"
    )


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_patch_false_wins_inflight_namespace_backfill(
    client: AsyncClient,
    db_session,
):
    """Startup namespace repair re-merges from the locked current config."""

    project = Project(
        name="namespace-backfill-race",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="acme-service",
        build_config={"automatic_strategic_cycles_enabled": True},
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    from app.task_locking import project_lock as real_project_lock

    backfill_reached_final_seam = asyncio.Event()
    release_backfill = asyncio.Event()

    @asynccontextmanager
    async def _delayed_final_lock(*args, **kwargs):
        # The batch SELECT is complete before the backfill enters this lock.
        backfill_reached_final_seam.set()
        await asyncio.wait_for(release_backfill.wait(), timeout=5)
        async with real_project_lock(*args, **kwargs) as locked_project:
            yield locked_project

    async def _run_backfill() -> dict:
        async with Session() as session:
            summary = await backfill_task_branch_namespace(
                session,
                project_id=project_id,
            )
            await session.commit()
            return summary

    with patch("app.task_locking.project_lock", new=_delayed_final_lock):
        backfill_task = asyncio.create_task(_run_backfill())
        await asyncio.wait_for(backfill_reached_final_seam.wait(), timeout=5)

        patch_response = await client.patch(
            f"/api/admin/projects/{project_id}",
            json={"build_config": {"automatic_strategic_cycles_enabled": False}},
        )
        assert patch_response.status_code == 200, patch_response.text

        release_backfill.set()
        summary = await asyncio.wait_for(backfill_task, timeout=10)
        assert summary["namespace_filled"] == 1

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed.build_config["task_branch_namespace"] == "acme-service"


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_patch_false_wins_inflight_bootstrap_recovery(
    client: AsyncClient,
    db_session,
):
    """Recovery refreshes stale config only after taking Project authority."""

    project = Project(
        name="bootstrap-recovery-race",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        build_config={
            "automatic_strategic_cycles_enabled": True,
            "repo_is_empty": False,
            "bootstrap_recovery_attempts": BOOTSTRAP_RECOVERY_MAX_ATTEMPTS,
        },
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    from app.task_locking import (
        acquire_automatic_task_creation_authority as real_creation_authority,
    )

    recovery_reached_authority = asyncio.Event()
    release_recovery = asyncio.Event()

    async def _delayed_creation_authority(*args, **kwargs):
        recovery_reached_authority.set()
        await asyncio.wait_for(release_recovery.wait(), timeout=5)
        return await real_creation_authority(*args, **kwargs)

    async def _run_recovery():
        async with Session() as session:
            stale_project = await session.get(Project, project_id)
            assert stale_project is not None
            result = await spawn_bootstrap_task(
                stale_project,
                session,
                spawn_reason="recovery_sweep",
                automatic_recovery=True,
            )
            await session.commit()
            return result

    with patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new=_delayed_creation_authority,
    ):
        recovery_task = asyncio.create_task(_run_recovery())
        await asyncio.wait_for(recovery_reached_authority.wait(), timeout=5)

        patch_response = await client.patch(
            f"/api/admin/projects/{project_id}",
            json={"build_config": {"automatic_strategic_cycles_enabled": False}},
        )
        assert patch_response.status_code == 200, patch_response.text

        release_recovery.set()
        result = await asyncio.wait_for(recovery_task, timeout=10)
        assert result.outcome == "not_eligible"
        assert result.reason == "bootstrap_recovery_exhausted"

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed.build_config["bootstrap_recovery_state"] == "exhausted"
    assert refreshed.build_config.get("bootstrap_recovery_exhausted_at") is not None


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_concurrent_pause_wins_bootstrap_recovery_without_side_effects(
    db_session,
):
    """A stale recovery candidate loses to pause before repository self-heal."""

    original_config = {"automatic_strategic_cycles_enabled": False}
    project = Project(
        name="paused-bootstrap-recovery",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        build_config=original_config,
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    pause_holds_lock = asyncio.Event()
    release_pause = asyncio.Event()

    async def _pause_first() -> None:
        async with Session() as session:
            locked = (
                await session.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            locked.paused = True
            session.add(locked)
            await session.flush()
            pause_holds_lock.set()
            await asyncio.wait_for(release_pause.wait(), timeout=5)
            await session.commit()

    self_heal = AsyncMock()
    with patch(
        "app.repo_bootstrap._self_heal_undetermined_emptiness",
        new=self_heal,
    ):
        pause_task = asyncio.create_task(_pause_first())
        await asyncio.wait_for(pause_holds_lock.wait(), timeout=5)
        result = await spawn_bootstrap_task(
            project,
            db_session,
            spawn_reason="recovery_sweep",
            automatic_recovery=True,
        )
        release_pause.set()
        await asyncio.wait_for(pause_task, timeout=5)

    assert result.outcome == "not_eligible"
    assert result.reason == "automatic_task_creation:project_unavailable"
    self_heal.assert_not_awaited()

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config == original_config
    task = (
        await db_session.execute(select(Task).where(Task.project_id == project_id))
    ).scalar_one_or_none()
    assert task is None


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_bootstrap_recovery_lock_precedes_self_heal_and_pause(db_session):
    """When recovery wins, pause cannot commit ahead of its Task/config write."""

    project = Project(
        name="bootstrap-recovery-lock-order",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        build_config={"automatic_strategic_cycles_enabled": False},
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    self_heal_started = asyncio.Event()
    release_self_heal = asyncio.Event()
    pause_attempted = asyncio.Event()
    pause_acquired = asyncio.Event()

    async def _delayed_self_heal(*_args, **_kwargs):
        self_heal_started.set()
        await asyncio.wait_for(release_self_heal.wait(), timeout=5)
        return "undetermined"

    async def _run_recovery() -> None:
        async with Session() as session:
            locked_candidate = await session.get(Project, project_id)
            assert locked_candidate is not None
            result = await spawn_bootstrap_task(
                locked_candidate,
                session,
                spawn_reason="recovery_sweep",
                automatic_recovery=True,
            )
            assert result.outcome == "spawned"
            await session.commit()

    async def _pause_after_spawn_lock() -> None:
        await asyncio.wait_for(self_heal_started.wait(), timeout=5)
        async with Session() as session:
            pause_attempted.set()
            locked = (
                await session.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            pause_acquired.set()
            committed_task = (
                await session.execute(
                    select(Task).where(Task.project_id == project_id)
                )
            ).scalar_one_or_none()
            assert committed_task is not None
            locked.paused = True
            session.add(locked)
            await session.commit()

    with patch(
        "app.repo_bootstrap._self_heal_undetermined_emptiness",
        new=AsyncMock(side_effect=_delayed_self_heal),
    ):
        recovery_task = asyncio.create_task(_run_recovery())
        await asyncio.wait_for(self_heal_started.wait(), timeout=5)
        pause_task = asyncio.create_task(_pause_after_spawn_lock())
        await asyncio.wait_for(pause_attempted.wait(), timeout=5)
        await asyncio.sleep(0.1)
        assert not pause_acquired.is_set()

        release_self_heal.set()
        await asyncio.wait_for(asyncio.gather(recovery_task, pause_task), timeout=10)

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.paused is True


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.parametrize(
    ("spawn_reason", "existing_config", "expected_state"),
    [
        ("operator_retry", {}, "operator_retry"),
        (
            "operator_refresh",
            {"bootstrap_completed_at": "2026-08-15T00:00:00+00:00"},
            "operator_refresh",
        ),
    ],
)
@pytest.mark.asyncio
async def test_patch_false_wins_inflight_operator_bootstrap_write(
    client: AsyncClient,
    db_session,
    spawn_reason: str,
    existing_config: dict,
    expected_state: str,
):
    """Explicit retry/refresh serialize config without inheriting pause policy."""

    project = Project(
        name=f"bootstrap-{spawn_reason}-race",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        build_config={
            "automatic_strategic_cycles_enabled": True,
            "repo_is_empty": False,
            "bootstrap_recovery_attempts": 2,
            "bootstrap_recovery_next_at": "2026-08-16T00:00:00+00:00",
            **existing_config,
        },
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    from app.task_locking import lock_project_or_429 as real_project_lock

    bootstrap_reached_final_seam = asyncio.Event()
    release_bootstrap = asyncio.Event()

    async def _delayed_project_lock(*args, **kwargs):
        bootstrap_reached_final_seam.set()
        await asyncio.wait_for(release_bootstrap.wait(), timeout=5)
        return await real_project_lock(*args, **kwargs)

    async def _run_bootstrap():
        async with Session() as session:
            stale_project = await session.get(Project, project_id)
            assert stale_project is not None
            result = await spawn_bootstrap_task(
                stale_project,
                session,
                spawn_reason=spawn_reason,
            )
            await session.commit()
            return result

    with patch("app.task_locking.lock_project_or_429", new=_delayed_project_lock):
        bootstrap_task = asyncio.create_task(_run_bootstrap())
        await asyncio.wait_for(bootstrap_reached_final_seam.wait(), timeout=5)

        patch_response = await client.patch(
            f"/api/admin/projects/{project_id}",
            json={"build_config": {"automatic_strategic_cycles_enabled": False}},
        )
        assert patch_response.status_code == 200, patch_response.text

        release_bootstrap.set()
        result = await asyncio.wait_for(bootstrap_task, timeout=10)
        assert result.outcome == "spawned"

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed.build_config["bootstrap_recovery_state"] == expected_state
    assert refreshed.build_config["bootstrap_recovery_attempts"] == 0
    assert "bootstrap_recovery_next_at" not in refreshed.build_config


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_patch_false_wins_inflight_async_landing_proof(
    client: AsyncClient,
    db_session,
):
    """Async landing stamps proof on a fresh config after an operator PATCH."""

    project = Project(
        name="landing-proof-race",
        user_id="test-user-123",
        integration_branch="automain",
        is_self_repo=False,
        build_config={"automatic_strategic_cycles_enabled": True},
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    task = Task(
        project_id=project.id,
        title="landing proof race",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    project_id = project.id
    task_id = task.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    from app.onboarding_proof import (
        lock_project_for_onboarding_proof as real_proof_lock,
    )

    landing_reached_final_seam = asyncio.Event()
    release_landing = asyncio.Event()

    async def _delayed_proof_lock(*args, **kwargs):
        landing_reached_final_seam.set()
        await asyncio.wait_for(release_landing.wait(), timeout=5)
        return await real_proof_lock(*args, **kwargs)

    async def _run_landing() -> bool:
        async with Session() as session:
            stale_project = await session.get(Project, project_id)
            stale_task = await session.get(Task, task_id)
            assert stale_project is not None
            assert stale_task is not None
            # Exercise the SQLAlchemy hazard explicitly: an unrelated dirty
            # Project scalar must neither autoflush before the non-blocking lock
            # nor be discarded by populate_existing's config refresh.
            stale_project.name = "landing proof renamed by caller"
            landed = await corroborate_and_apply_merged_landed_signal(
                stale_task,
                stale_project,
                session,
                base_branch="automain",
                hotfix_lane=False,
                merge_commit_sha="a" * 40,
            )
            await session.commit()
            return landed

    with (
        patch(
            "app.onboarding_proof.lock_project_for_onboarding_proof",
            new=_delayed_proof_lock,
        ),
        patch(
            "app.landed_signal._stamp_changed_files_before_landing",
            new=AsyncMock(),
        ),
        patch(
            "app.acceptance_gate.corroborate_acceptance_for_landing",
            new=AsyncMock(),
        ),
        patch(
            "app.landed_signal._verify_finding_clearance_before_landing",
            new=AsyncMock(),
        ),
        patch(
            "app.module_finding_chain.record_merged_pr_finding_chains",
            new=AsyncMock(),
        ),
    ):
        landing_task = asyncio.create_task(_run_landing())
        await asyncio.wait_for(landing_reached_final_seam.wait(), timeout=5)

        patch_response = await client.patch(
            f"/api/admin/projects/{project_id}",
            json={"build_config": {"automatic_strategic_cycles_enabled": False}},
        )
        assert patch_response.status_code == 200, patch_response.text

        release_landing.set()
        assert await asyncio.wait_for(landing_task, timeout=10) is True

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.name == "landing proof renamed by caller"
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed.build_config.get("first_pr_merged_at") is not None


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_contended_landing_skips_proof_without_blocking_core_outcome(db_session):
    """A held Project row defers proof only; the Task still lands promptly."""

    project = Project(
        name="landing-proof-contention",
        user_id="test-user-123",
        integration_branch="automain",
        build_config={"automatic_strategic_cycles_enabled": True},
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="landing proof contention",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    project_id = project.id
    task_id = task.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    operator_holds_lock = asyncio.Event()
    release_operator = asyncio.Event()

    async def _operator_disable() -> None:
        async with Session() as session:
            locked = (
                await session.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            locked.build_config = {"automatic_strategic_cycles_enabled": False}
            session.add(locked)
            await session.flush()
            operator_holds_lock.set()
            await asyncio.wait_for(release_operator.wait(), timeout=5)
            await session.commit()

    async def _land_while_contended() -> bool:
        async with Session() as session:
            stale_project = await session.get(Project, project_id)
            stale_task = await session.get(Task, task_id)
            assert stale_project is not None
            assert stale_task is not None
            await asyncio.wait_for(operator_holds_lock.wait(), timeout=5)
            landed = await corroborate_and_apply_merged_landed_signal(
                stale_task,
                stale_project,
                session,
                base_branch="automain",
                hotfix_lane=False,
                merge_commit_sha="b" * 40,
            )
            await session.commit()
            return landed

    with (
        patch(
            "app.landed_signal._stamp_changed_files_before_landing",
            new=AsyncMock(),
        ),
        patch(
            "app.acceptance_gate.corroborate_acceptance_for_landing",
            new=AsyncMock(),
        ),
        patch(
            "app.landed_signal._verify_finding_clearance_before_landing",
            new=AsyncMock(),
        ),
        patch(
            "app.module_finding_chain.record_merged_pr_finding_chains",
            new=AsyncMock(),
        ),
    ):
        operator_task = asyncio.create_task(_operator_disable())
        landing_task = asyncio.create_task(_land_while_contended())
        assert await asyncio.wait_for(landing_task, timeout=5) is True
        release_operator.set()
        await asyncio.wait_for(operator_task, timeout=5)

    db_session.expire_all()
    refreshed_project = await db_session.get(Project, project_id)
    refreshed_task = await db_session.get(Task, task_id)
    assert refreshed_project is not None
    assert refreshed_task is not None
    assert refreshed_project.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed_project.build_config.get("first_pr_merged_at") is None
    assert (refreshed_task.context or {}).get("landed_on_integration") is True


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_noop_discovery_keeps_project_usable_for_followup_redetection(db_session):
    """A fresh-state no-op must not rollback/expire the caller's Project."""

    project = Project(
        name="build-config-noop-chain",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        build_config={"repo_is_empty": True, "directories": []},
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with Session() as writer_session:
        stale_project = await writer_session.get(Project, project_id)
        assert stale_project is not None

        async with Session() as operator_session:
            current = await operator_session.get(Project, project_id)
            assert current is not None
            current.build_config = {
                "repo_is_empty": True,
                "directories": [{"path": ".", "type": "python"}],
                "automatic_strategic_cycles_enabled": False,
            }
            operator_session.add(current)
            await operator_session.commit()

        persisted = await persist_discovered_build_commands(
            stale_project,
            writer_session,
            [
                {
                    "path": ".",
                    "type": "python",
                    "test_cmd": "pytest",
                    "executed_successfully": True,
                }
            ],
        )
        assert persisted is False
        assert stale_project.build_config["automatic_strategic_cycles_enabled"] is False

        with (
            patch(
                "app.github.get_repo_installation_token",
                new=AsyncMock(return_value="token"),
            ),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new=AsyncMock(
                    return_value={
                        "directories": [
                            {"path": ".", "type": "python", "test_cmd": "pytest"}
                        ],
                        "repo_is_empty": False,
                        "source_file_count": 4,
                        "has_ci": True,
                    }
                ),
            ),
        ):
            assert (
                await maybe_refresh_empty_build_config(
                    stale_project,
                    writer_session,
                )
                is True
            )

    db_session.expire_all()
    refreshed = await db_session.get(Project, project_id)
    assert refreshed is not None
    assert refreshed.build_config["automatic_strategic_cycles_enabled"] is False
    assert refreshed.build_config["repo_is_empty"] is False
