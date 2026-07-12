"""
Tests for admin_runners.py — per-runner fleet stats.

Focus: the cloud-cost-avoided rollup on
``GET /api/admin/projects/{project_id}/runners/{runner_id}/stats``, which
surfaces the dollar value the self-hosted fleet saved by running terminal
compute jobs that would otherwise have accrued on-demand cloud compute cost.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.compute_backends import cloud_equivalent_cost_usd
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    Project,
    ProjectMember,
)

TEST_USER_ID = "test-user-123"


async def _make_project(session, name="Runner Stats Project"):
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await session.commit()
    return project


def _pin(runner_id) -> dict:
    """polling_metadata that pins a compute job to a runner (see runner_stats)."""
    return {"runner_target_id": str(runner_id)}


# ═══════════════════════════════════════════════════════════════════════════════
# cloud_equivalent_cost_usd unit behavior (pure helper, no DB)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCloudEquivalentCostHelper:
    def _terminal_gpu_job(self, **overrides) -> ComputeJob:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        kwargs = {
            "project_id": None,
            "backend": "runner-subprocess",
            "status": ComputeJobStatus.SUCCEEDED,
            "accelerator_type": "T4",
            "accelerator_count": 1,
            "started_at": start,
            "completed_at": start + timedelta(hours=1),
        }
        kwargs.update(overrides)
        return ComputeJob(**kwargs)

    def test_self_hosted_terminal_gpu_job_prices_positive(self):
        avoided = cloud_equivalent_cost_usd(self._terminal_gpu_job())
        assert avoided is not None
        assert avoided > 0

    def test_cloud_backend_job_returns_none(self):
        # Blueprint actually paid for a cloud-backend job — nothing was avoided.
        job = self._terminal_gpu_job(backend="gcp")
        assert cloud_equivalent_cost_usd(job) is None

    def test_non_terminal_job_returns_none(self):
        job = self._terminal_gpu_job(status=ComputeJobStatus.RUNNING)
        assert cloud_equivalent_cost_usd(job) is None

    def test_missing_timestamps_returns_none(self):
        assert cloud_equivalent_cost_usd(self._terminal_gpu_job(started_at=None)) is None
        assert cloud_equivalent_cost_usd(self._terminal_gpu_job(completed_at=None)) is None

    def test_negative_or_zero_window_returns_none(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = self._terminal_gpu_job(started_at=start, completed_at=start)
        assert cloud_equivalent_cost_usd(job) is None

    def test_unknown_accelerator_returns_none_not_raise(self):
        # A malformed shape must not raise — it simply contributes nothing.
        job = self._terminal_gpu_job(accelerator_type="NOT-A-GPU")
        assert cloud_equivalent_cost_usd(job) is None


# ═══════════════════════════════════════════════════════════════════════════════
# runner_stats endpoint — cloud-cost-avoided rollup
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunnerStatsCloudCostAvoided:
    @pytest.mark.asyncio
    async def test_terminal_self_hosted_job_contributes_to_avoided(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="a" * 64, hostname="host1", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            accelerator_type="T4",
            accelerator_count=1,
            started_at=start,
            completed_at=start + timedelta(hours=1),
            polling_metadata=_pin(runner.id),
        )
        db_session.add(job)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # A single terminal T4 job with an honest 1h window backs the avoided total.
        assert "cloud_cost_avoided_usd" in body
        assert "cloud_cost_avoided_basis" in body
        assert body["cloud_cost_avoided_basis"] == 1
        assert body["cloud_cost_avoided_usd"] > 0
        # Claim-latency rollup fields are always present on the response.
        assert "claim_latency_sample_count" in body

    @pytest.mark.asyncio
    async def test_nonterminal_job_excluded_from_avoided(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="b" * 64, hostname="host2", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # A non-terminal job — no settled completed_at window to price, so it
        # contributes neither to the avoided total nor to the honest-window basis.
        running_job = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.RUNNING,
            accelerator_type="T4",
            accelerator_count=1,
            started_at=start,
            polling_metadata=_pin(runner.id),
        )
        db_session.add(running_job)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cloud_cost_avoided_usd"] == pytest.approx(0.0)
        assert body["cloud_cost_avoided_basis"] == 0

    @pytest.mark.asyncio
    async def test_malformed_job_does_not_500_and_contributes_zero(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="c" * 64, hostname="host3", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Terminal, self-hosted, but with an unknown accelerator (unpriceable) and
        # another with missing started_at — neither may raise nor contribute.
        bad_accel = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            accelerator_type="NOT-A-GPU",
            accelerator_count=1,
            started_at=start,
            completed_at=start + timedelta(hours=1),
            polling_metadata=_pin(runner.id),
        )
        no_start = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=None,
            completed_at=start + timedelta(hours=1),
            polling_metadata=_pin(runner.id),
        )
        db_session.add_all([bad_accel, no_start])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The unpriceable-accelerator job has an honest window (counts toward the
        # basis) but prices to nothing; the missing-start job is excluded entirely.
        # Neither raises, so the endpoint returns a zero avoided total, not a 500.
        assert body["cloud_cost_avoided_usd"] == pytest.approx(0.0)
        assert body["cloud_cost_avoided_basis"] == 1

    @pytest.mark.asyncio
    async def test_unknown_runner_returns_404(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{uuid.uuid4()}/stats"
        )
        assert resp.status_code == 404
