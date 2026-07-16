"""Attempt-24 operator-flow end-to-end proofs.

Covers:
  * Runner secondary project grants: a runner enrolled in project A can serve
    project B via ``RunnerProjectBinding`` allow-list entries.
  * ``validate_compute_runner_target_enrollment`` accepts a secondary-bound runner.
  * Hard-pin UUID/hostname resolution accepts only allow-listed runners.
  * ``_resolve_override_runner`` resolves a runner on a secondary project's allow-list.
  * Strategic discovery, status, affinity, attention, and revision reads agree.
  * ``upload_compute_job_viewable`` accepts uploads for compute jobs in a secondary project.
"""

import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth as auth_mod
from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.compute_experiment_contract import validate_compute_runner_target_enrollment
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    _utcnow,
)
from app.runner_project_binding import (
    add_binding,
    allowed_project_ids,
)


TEST_USER_ID = "test-user-123"


async def _make_project(session: AsyncSession, name: str) -> Project:
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


async def _make_runner(
    session: AsyncSession, project_id: uuid.UUID, **kwargs
) -> tuple[LocalRunner, str]:
    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project_id,
        token_hash=auth_mod.hash_runner_token(token),
        last_seen_at=kwargs.pop("last_seen_at", _utcnow()),
        capabilities=kwargs.pop(
            "capabilities",
            {"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        ),
        **kwargs,
    )
    session.add(runner)
    await session.commit()
    await session.refresh(runner)
    return runner, token


class TestAttempt24SecondaryRunnerAndMultifileToolFlow:
    """A runner enrolled in project A serves project B end-to-end.

    Exercises the authorization and Strategic read/discovery seams fixed to
    honour secondary project grants, plus mid-run viewable-file upload.
    """

    async def test_secondary_bound_runner_target_validation(
        self, db_session: AsyncSession
    ):
        """``validate_compute_runner_target_enrollment`` accepts a secondary-bound runner.

        Before the fix, this rejected a runner whose ``project_id`` did not match
        the target project — even when the runner had an explicit
        ``RunnerProjectBinding`` to that project. After the fix, the canonical
        ``allowed_project_ids`` resolver defines membership.
        """
        project_a = await _make_project(db_session, name="Target Val A")
        project_b = await _make_project(db_session, name="Target Val B")

        # Runner enrolled in A, bound to B via explicit allow-list row.
        runner, _token = await _make_runner(db_session, project_a.id)
        await add_binding(db_session, runner.id, project_b.id)
        assert await allowed_project_ids(db_session, runner) == {
            project_a.id,
            project_b.id,
        }

        # Validation for project A (enrollment) — always ok.
        assert (
            await validate_compute_runner_target_enrollment(
                db_session,
                project_a.id,
                {"runner_id": str(runner.id)},
            )
        ) is None

        # Validation for project B (secondary binding) — must now succeed.
        result_b = await validate_compute_runner_target_enrollment(
            db_session,
            project_b.id,
            {"runner_id": str(runner.id)},
        )
        assert result_b is None, (
            f"secondary-bound runner must pass validation; got {result_b}"
        )

    async def test_secondary_bound_runner_target_validation_unbound_rejected(
        self, db_session: AsyncSession
    ):
        """A runner NOT on a project's allow-list is still rejected at authoring time."""
        project_a = await _make_project(db_session, name="Unbound Val A")
        project_c = await _make_project(db_session, name="Unbound Val C")

        runner, _token = await _make_runner(db_session, project_a.id)
        # No binding to project C — validation must reject.
        result = await validate_compute_runner_target_enrollment(
            db_session,
            project_c.id,
            {"runner_id": str(runner.id)},
        )
        assert result is not None
        assert result["error"] == "runner_target_invalid"
        assert result["reason_code"] == "not_enrolled"

    async def test_secondary_bound_runner_viewable_upload(
        self, db_session: AsyncSession, client: AsyncClient, mocker
    ):
        """A runner can upload viewable files for a compute job in a secondary project.

        Before the fix the upload endpoint filtered on
        ``ComputeJob.project_id == runner.project_id``, so a secondary-project
        compute job returned 404 at the scope guard.
        """
        project_a = await _make_project(db_session, name="Viewable A")
        project_b = await _make_project(db_session, name="Viewable B")

        runner, token = await _make_runner(db_session, project_a.id)
        await add_binding(db_session, runner.id, project_b.id)
        assert project_b.id in await allowed_project_ids(db_session, runner)

        # A RUNNING compute job in project B, pinned to this runner.
        job = ComputeJob(
            project_id=project_b.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={
                "runner_target_id": str(runner.id),
                "command": "echo b",
            },
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        # Runner heartbeat so it appears live for the endpoint's runner dependency.
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": [], "active_compute_job_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        fake_uri = (
            f"gs://test-bucket/compute-jobs/{job.id}/"
            "artifacts/viewable/secondary.png"
        )
        upload = mocker.patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=fake_uri,
        )
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/viewable",
            data={"filename": "secondary.png"},
            files={"file": ("secondary.png", b"real-image-bytes", "image/png")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["entry"]["storage_uri"] == fake_uri
        upload.assert_called_once()

        # An otherwise identical job in an unbound project stays hidden even
        # with a valid multipart body, proving the denial at the scope guard.
        project_c = await _make_project(db_session, name="Viewable Unbound")
        unbound_job = ComputeJob(
            project_id=project_c.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={
                "runner_target_id": str(runner.id),
                "command": "echo c",
            },
        )
        db_session.add(unbound_job)
        await db_session.commit()
        await db_session.refresh(unbound_job)
        denied = await client.post(
            f"/api/runner/compute-job/{unbound_job.id}/viewable",
            data={"filename": "secondary.png"},
            files={"file": ("secondary.png", b"real-image-bytes", "image/png")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert denied.status_code == 404, denied.text

    async def test_secondary_bound_runner_override_resolution(
        self, db_session: AsyncSession
    ):
        """``_resolve_override_runner`` resolves a runner on a secondary project's allow-list.

        Before the fix this required ``runner.project_id == project_id``, so
        a runner bound via ``RunnerProjectBinding`` could not be selected as
        a strategic-resident session override.
        """
        from app.strategic_resident_session import _resolve_override_runner

        project_a = await _make_project(db_session, name="Override A")
        project_b = await _make_project(db_session, name="Override B")

        runner, _token = await _make_runner(db_session, project_a.id)
        await add_binding(db_session, runner.id, project_b.id)

        # Resolution for project A (enrollment) — always ok.
        found_a = await _resolve_override_runner(
            db_session, project_a.id, str(runner.id)
        )
        assert found_a is not None

        # Resolution for project B (secondary binding) — must now succeed.
        found_b = await _resolve_override_runner(
            db_session, project_b.id, str(runner.id)
        )
        assert found_b is not None, (
            "secondary-bound runner must resolve as override for project B"
        )

        # A deleted runner is never returned.
        runner.status = "deleted"
        db_session.add(runner)
        await db_session.commit()
        found_deleted = await _resolve_override_runner(
            db_session, project_b.id, str(runner.id)
        )
        assert found_deleted is None

    async def test_secondary_bound_runner_hard_pin_uuid_hostname_and_unbound(
        self, db_session: AsyncSession
    ):
        """Hard pins share allow-list membership for UUID and hostname resolution."""
        from app.admin import _resolve_forced_runner_target

        project_a = await _make_project(db_session, name="Hard Pin A")
        project_b = await _make_project(db_session, name="Hard Pin B")
        project_c = await _make_project(db_session, name="Hard Pin Unbound")
        runner, _token = await _make_runner(
            db_session,
            project_a.id,
            hostname="secondary-dgx",
        )
        await add_binding(db_session, runner.id, project_b.id)

        assert await _resolve_forced_runner_target(
            db_session, project_b.id, str(runner.id)
        ) == str(runner.id)
        assert await _resolve_forced_runner_target(
            db_session, project_b.id, runner.hostname
        ) == str(runner.id)

        for target in (str(runner.id), runner.hostname):
            with pytest.raises(HTTPException) as exc_info:
                await _resolve_forced_runner_target(
                    db_session,
                    project_c.id,
                    target,
                )
            assert exc_info.value.status_code == 422
            assert exc_info.value.detail["error"] == "forced_runner_not_found"

    async def test_secondary_bound_runner_strategic_discovery_and_reporting(
        self, db_session: AsyncSession
    ):
        """Every Strategic discovery/read model honors a secondary grant."""
        from app.operator_attention import _load_stale_runner_pins
        from app.routers.admin_project_strategic_session import (
            _compute_strategic_state_fingerprints,
        )
        from app.strategic_local_runner import (
            _load_local_runner_status,
            _resolve_local_continuation_runner_affinity,
        )
        from app.strategic_resident_session import (
            _auto_select_local_runner,
            build_resident_runner_options,
        )
        from app.strategic_runtime_work import build_strategic_runtime_summary
        from app.workbench_state import (
            STRATEGIC_LOCAL_CONTINUATION_KEY,
            _valid_preferred_runner_ids,
        )

        project_a = await _make_project(db_session, name="Strategic Read A")
        project_b = await _make_project(db_session, name="Strategic Read B")
        runner, _token = await _make_runner(
            db_session,
            project_a.id,
            hostname="shared-strategic",
            capabilities={
                "task_types": ["strategic_review"],
                "has_gpu": True,
                "max_concurrent": 2,
                "agent_available": True,
            },
        )
        await add_binding(db_session, runner.id, project_b.id)

        now = _utcnow().isoformat()
        project_b.vision = {
            STRATEGIC_LOCAL_CONTINUATION_KEY: {
                "version": 1,
                "active": True,
                "updated_at": now,
                "last_seen_at": now,
                "preferred_runner_id": str(runner.id),
            }
        }
        db_session.add(project_b)
        await db_session.commit()

        assert await _auto_select_local_runner(
            db_session, project_b.id
        ) == str(runner.id)
        options = await build_resident_runner_options(db_session, project_b.id)
        assert [option["id"] for option in options] == [str(runner.id)]
        assert options[0]["eligible"] is True
        assert await _valid_preferred_runner_ids(db_session, project_b.id) == {
            str(runner.id)
        }

        status = await _load_local_runner_status(db_session, project_b.id)
        assert status["ever_registered"] is True
        assert status["active_total"] == 1
        assert status["active_gpu"] == 1
        assert await _resolve_local_continuation_runner_affinity(
            db_session,
            project_b.id,
            "strategic_review",
        ) == str(runner.id)

        runtime = await build_strategic_runtime_summary(
            db_session,
            project_b.id,
            active_task=None,
            project_vision=project_b.vision,
        )
        local_runner = runtime["local_runner"]
        assert local_runner["strategic_runner_count"] == 1
        assert local_runner["live_runner_count"] == 1
        assert local_runner["runners"][0]["id"] == str(runner.id)

        fingerprints = await _compute_strategic_state_fingerprints(
            db_session,
            project_b.id,
            TEST_USER_ID,
        )
        assert fingerprints["runner_count"] == 1
        assert fingerprints["live_runner_count"] == 1
        assert fingerprints["runner_max_seen"] == str(runner.last_seen_at)

        # A live secondary-bound runner pinned to a provisioning task is present,
        # so operator attention must not misclassify the pin as missing/stale.
        pinned = Task(
            project_id=project_b.id,
            payloads=[{"type": "strategic_review", "prompt": "continue"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(pinned)
        await db_session.commit()
        stale_count, stale_preview = await _load_stale_runner_pins(
            db_session,
            project_b.id,
        )
        assert stale_count == 0
        assert stale_preview == []

    async def test_secondary_runner_unified_ledger_after_cache_fanout(
        self, db_session: AsyncSession
    ):
        """Dispatch cache fans a multi-project runner into both project buckets.

        Verifies that the dispatch cache (``_load_active_runners_cache``) already
        registers the runner under EVERY allowed project — the existing
        production behaviour that the authorization and Strategic read seams
        above now also honour.
        """
        from app import orchestrator as orch

        project_a = await _make_project(db_session, name="Cache A")
        project_b = await _make_project(db_session, name="Cache B")

        runner, _token = await _make_runner(db_session, project_a.id)
        await add_binding(db_session, runner.id, project_b.id)

        cache = await orch._load_active_runners_cache(db_session)
        runner_ids_a = {r.id for r in cache.get(project_a.id, [])}
        runner_ids_b = {r.id for r in cache.get(project_b.id, [])}

        assert runner.id in runner_ids_a, "runner must appear under enrollment project"
        assert runner.id in runner_ids_b, (
            "runner must appear under secondary-bound project"
        )

    async def test_secondary_bound_dead_runner_attention_item_routes_to_viewing_project(
        self, db_session: AsyncSession
    ):
        """Dead-runner attention item for a secondary-bound runner routes to project B.

        When a runner enrolled in project A is allowed by project B (via
        ``RunnerProjectBinding``) and that runner goes dead, the attention item
        surfaced in project B's feed must stamp ``ref.project_id`` and
        ``target.project_id`` with project B's id — not project A's — so
        navigation stays within project B's context.
        """
        from datetime import timedelta

        from app.operator_attention import _dead_runner_attention_item, _load_dead_runners

        project_a = await _make_project(db_session, name="Dead Runner A")
        project_b = await _make_project(db_session, name="Dead Runner B")

        stale_seen_at = _utcnow() - timedelta(hours=3)
        runner, _token = await _make_runner(
            db_session,
            project_a.id,
            hostname="dead-shared",
            last_seen_at=stale_seen_at,
            status="inactive",
        )
        await add_binding(db_session, runner.id, project_b.id)

        # _load_dead_runners scoped to project B must surface this runner.
        _, _, dead_b = await _load_dead_runners(db_session, project_b.id)
        assert any(r.id == runner.id for r in dead_b), (
            "secondary-bound dead runner must appear in project B's dead-runner list"
        )

        # The serialized item must route to project B, not project A.
        item = _dead_runner_attention_item(runner, project_id=project_b.id)
        assert item["ref"]["project_id"] == str(project_b.id), (
            f"ref.project_id must be project B ({project_b.id}), "
            f"got {item['ref']['project_id']}"
        )
        assert item["target"]["project_id"] == str(project_b.id), (
            f"target.project_id must be project B ({project_b.id}), "
            f"got {item['target']['project_id']}"
        )
        # Sanity: the runner is enrolled in A, so these would differ without the fix.
        assert str(runner.project_id) == str(project_a.id)
        assert item["ref"]["project_id"] != str(project_a.id), (
            "ref.project_id must NOT be the enrollment project A"
        )
