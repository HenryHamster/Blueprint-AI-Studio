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
from app.routers.admin_runners import _normalize_gpus_per_job, _runner_config_view
from app.time_utils import _utcnow
from app.runner_config import (
    GPUS_PER_JOB_MAX,
    GPUS_PER_JOB_MIN,
    config_generation,
    live_safe_config,
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

    @pytest.mark.asyncio
    async def test_runner_stats_recent_outcomes_include_terminal_reason_and_exit_code(
        self, client: AsyncClient, db_session
    ):
        # A FAILED runner job must carry its terminal_reason and exit_code into the
        # fleet recent-outcomes list so the operator sees *why* a job failed without
        # leaving the fleet panel. A SUCCEEDED/legacy job yields None for both.
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="d" * 64, hostname="host4", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        failed = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.FAILED,
            started_at=start,
            completed_at=start + timedelta(minutes=5),
            terminal_reason="process exited non-zero",
            polling_metadata={**_pin(runner.id), "exit_code": 137},
        )
        succeeded = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=start,
            completed_at=start + timedelta(minutes=1),
            polling_metadata=_pin(runner.id),
        )
        db_session.add_all([failed, succeeded])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        outcomes = {o["job_id"]: o for o in resp.json()["last_n_outcomes"]}

        failed_row = outcomes[str(failed.id)]
        assert failed_row["terminal_reason"] == "process exited non-zero"
        assert failed_row["exit_code"] == 137

        # A success row (no terminal_reason, no exit_code in metadata) surfaces None
        # for both — never a stale placeholder or a non-int leak.
        succeeded_row = outcomes[str(succeeded.id)]
        assert succeeded_row["terminal_reason"] is None
        assert succeeded_row["exit_code"] is None

    @pytest.mark.asyncio
    async def test_runner_stats_failed_outcome_includes_redacted_command_log_tail(
        self, client: AsyncClient, db_session
    ):
        # A FAILED runner job with a captured command log surfaces a redacted,
        # length-bounded tail inline on its recent-outcome row so an operator can
        # triage *why* it failed without leaving the fleet view. A SUCCEEDED job
        # never carries a tail even when it captured a log.
        from app.routers.admin_runners import _COMMAND_LOG_TAIL_MAX_CHARS

        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="e" * 64, hostname="host5", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        # Log longer than the tail cap, with a credential that must be redacted.
        prefix = "x" * (_COMMAND_LOG_TAIL_MAX_CHARS + 500)
        raw_text = f"{prefix}\nAuthorization: Bearer sk-supersecret-token-value\nboom"
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        failed = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.FAILED,
            started_at=start,
            completed_at=start + timedelta(minutes=5),
            terminal_reason="process exited non-zero",
            polling_metadata={
                **_pin(runner.id),
                "command_log": {
                    "text": raw_text,
                    "truncated": True,
                    "byte_count": len(raw_text.encode()),
                },
            },
        )
        succeeded = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=start,
            completed_at=start + timedelta(minutes=1),
            polling_metadata={
                **_pin(runner.id),
                "command_log": {"text": "all good", "truncated": False, "byte_count": 8},
            },
        )
        db_session.add_all([failed, succeeded])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        outcomes = {o["job_id"]: o for o in resp.json()["last_n_outcomes"]}

        tail = outcomes[str(failed.id)]["command_log_tail"]
        assert tail is not None
        # Bounded to the tail cap (keeps the fleet payload lean).
        assert len(tail) <= _COMMAND_LOG_TAIL_MAX_CHARS
        # It is a TAIL — the trailing failure line survives, the head is elided.
        assert tail.endswith("boom")
        assert tail.startswith("x")
        # Credentials are scrubbed before the tail reaches the operator surface.
        assert "supersecret" not in tail

        # A SUCCEEDED row never carries a tail even with a captured log.
        assert outcomes[str(succeeded.id)]["command_log_tail"] is None

    async def test_runner_stats_tail_reads_runner_command_log_key(
        self, client: AsyncClient, db_session
    ):
        # Runner-subprocess jobs stamp their terminal log under
        # polling_metadata["runner_command_log"] (see runner.report_compute_job),
        # NOT the cloud "command_log" key. The tail must fall back to that key or
        # the exact fleet diagnostic the operator triages with is always None.
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="f" * 64, hostname="host6", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        raw_text = "setup log line\nAuthorization: Bearer sk-runner-secret-value\nboom"
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        failed = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.FAILED,
            started_at=start,
            completed_at=start + timedelta(minutes=5),
            terminal_reason="process exited non-zero",
            polling_metadata={
                **_pin(runner.id),
                "runner_command_log": {"text": raw_text, "truncated": False},
            },
        )
        db_session.add(failed)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        outcomes = {o["job_id"]: o for o in resp.json()["last_n_outcomes"]}

        tail = outcomes[str(failed.id)]["command_log_tail"]
        assert tail is not None
        assert tail.endswith("boom")
        # Credentials are scrubbed before the tail reaches the operator surface.
        assert "runner-secret" not in tail


# ═══════════════════════════════════════════════════════════════════════════════
# runner_stats endpoint — attribution by authoritative ran_on_runner_id claim
# (with legacy runner_target_id pin fallback)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunnerStatsAttribution:
    @pytest.mark.asyncio
    async def test_job_attributed_by_ran_on_runner_id_over_pin(
        self, client: AsyncClient, db_session
    ):
        # A job the runner AUTHORITATIVELY claimed (ran_on_runner_id) is counted for
        # that runner even when its dispatch pin (runner_target_id) points at a
        # DIFFERENT runner. Metrics follow the runner that actually ran the job.
        project = await _make_project(db_session)
        claimer = LocalRunner(
            project_id=project.id, token_hash="f" * 64, hostname="claimer", status="active"
        )
        other = LocalRunner(
            project_id=project.id, token_hash="9" * 64, hostname="other", status="active"
        )
        db_session.add_all([claimer, other])
        await db_session.commit()
        await db_session.refresh(claimer)
        await db_session.refresh(other)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=start,
            completed_at=start + timedelta(minutes=3),
            ran_on_runner_id=claimer.id,
            # Pin points at `other`, but the claimer actually ran it.
            polling_metadata=_pin(other.id),
        )
        db_session.add(job)
        await db_session.commit()

        # Counted for the runner that CLAIMED it, not the pinned target.
        claimer_body = (
            await client.get(
                f"/api/admin/projects/{project.id}/runners/{claimer.id}/stats"
            )
        ).json()
        assert claimer_body["jobs_run_count"] == 1
        assert claimer_body["jobs_succeeded_count"] == 1
        assert {o["job_id"] for o in claimer_body["last_n_outcomes"]} == {str(job.id)}

        # NOT counted for the pinned-but-not-claiming runner.
        other_body = (
            await client.get(
                f"/api/admin/projects/{project.id}/runners/{other.id}/stats"
            )
        ).json()
        assert other_body["jobs_run_count"] == 0
        assert other_body["last_n_outcomes"] == []

    @pytest.mark.asyncio
    async def test_job_attributed_by_ran_on_runner_id_without_any_pin(
        self, client: AsyncClient, db_session
    ):
        # A claimed job with NO runner_target_id pin at all is still attributed via
        # ran_on_runner_id — the authoritative record needs no dispatch pin.
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="7" * 64, hostname="nopin", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=start,
            completed_at=start + timedelta(minutes=2),
            ran_on_runner_id=runner.id,
            polling_metadata={},  # no runner_target_id pin
        )
        db_session.add(job)
        await db_session.commit()

        body = (
            await client.get(
                f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
            )
        ).json()
        assert body["jobs_run_count"] == 1
        assert {o["job_id"] for o in body["last_n_outcomes"]} == {str(job.id)}

    @pytest.mark.asyncio
    async def test_legacy_null_ran_on_runner_id_falls_back_to_pin(
        self, client: AsyncClient, db_session
    ):
        # A legacy row predating the column (ran_on_runner_id IS NULL) is still
        # attributed by its runner_target_id pin — historical attribution preserved.
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="6" * 64, hostname="legacy", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=start,
            completed_at=start + timedelta(minutes=4),
            ran_on_runner_id=None,  # legacy: never stamped
            polling_metadata=_pin(runner.id),
        )
        db_session.add(job)
        await db_session.commit()

        body = (
            await client.get(
                f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
            )
        ).json()
        assert body["jobs_run_count"] == 1
        assert {o["job_id"] for o in body["last_n_outcomes"]} == {str(job.id)}

    @pytest.mark.asyncio
    async def test_pin_no_longer_attributes_once_claimed_elsewhere(
        self, client: AsyncClient, db_session
    ):
        # A legacy pin does NOT double-count a job that carries a (non-NULL)
        # ran_on_runner_id for a different runner: the NULL-fallback clause only
        # fires when ran_on_runner_id IS NULL, so a claimed job is attributed once.
        project = await _make_project(db_session)
        pinned = LocalRunner(
            project_id=project.id, token_hash="5" * 64, hostname="pinned", status="active"
        )
        claimer = LocalRunner(
            project_id=project.id, token_hash="4" * 64, hostname="realclaimer", status="active"
        )
        db_session.add_all([pinned, claimer])
        await db_session.commit()
        await db_session.refresh(pinned)
        await db_session.refresh(claimer)

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = ComputeJob(
            project_id=project.id,
            backend="runner-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=start,
            completed_at=start + timedelta(minutes=1),
            ran_on_runner_id=claimer.id,
            polling_metadata=_pin(pinned.id),
        )
        db_session.add(job)
        await db_session.commit()

        pinned_body = (
            await client.get(
                f"/api/admin/projects/{project.id}/runners/{pinned.id}/stats"
            )
        ).json()
        assert pinned_body["jobs_run_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Live-safe runner config: operator PATCH normalization + desired-vs-applied view
# (pure helpers, no DB) — the orchestrator half of the two-way heartbeat config
# channel that lets an operator reconfigure a LIVE runner (concurrency, task-type
# allowlist, GPU-index allowlist) without SSH + daemon restart.
# ═══════════════════════════════════════════════════════════════════════════════


class TestGpuIndicesAllowedNormalization:
    """``_normalize_gpu_indices_allowed`` is the operator-input validator for the
    GPU-index allowlist pushed to the daemon over the heartbeat. It must accept
    the ``"all"`` sentinel, coerce a list to deduped/sorted non-negative ints
    (so storage — and therefore the config-generation hash — is order-stable),
    and reject malformed shapes so a bad PATCH never silently corrupts the
    live-safe config."""

    def test_all_sentinel_passes_through(self):
        from app.routers.admin_runners import _normalize_gpu_indices_allowed

        assert _normalize_gpu_indices_allowed("all") == "all"

    def test_list_is_deduped_and_sorted(self):
        from app.routers.admin_runners import _normalize_gpu_indices_allowed

        assert _normalize_gpu_indices_allowed([2, 0, 2, 1]) == [0, 1, 2]

    def test_empty_list_is_a_valid_restriction(self):
        from app.routers.admin_runners import _normalize_gpu_indices_allowed

        # An empty list is a deliberate "no GPU indices allowed", not an error.
        assert _normalize_gpu_indices_allowed([]) == []

    def test_negative_index_rejected(self):
        from app.routers.admin_runners import _normalize_gpu_indices_allowed

        with pytest.raises(ValueError):
            _normalize_gpu_indices_allowed([0, -1])

    def test_non_int_entry_rejected(self):
        from app.routers.admin_runners import _normalize_gpu_indices_allowed

        with pytest.raises(ValueError):
            _normalize_gpu_indices_allowed([0, "gpu-1"])

    def test_bool_entry_rejected(self):
        from app.routers.admin_runners import _normalize_gpu_indices_allowed

        # bool is an int subclass — must be rejected explicitly so True/False
        # cannot masquerade as index 1/0.
        with pytest.raises(ValueError):
            _normalize_gpu_indices_allowed([True])

    def test_scalar_non_list_rejected(self):
        from app.routers.admin_runners import _normalize_gpu_indices_allowed

        with pytest.raises(ValueError):
            _normalize_gpu_indices_allowed(3)


class TestRunnerConfigView:
    """``_runner_config_view`` renders the desired-vs-applied LIVE-SAFE config the
    Fleet UI shows. ``config_pending`` must be True whenever the daemon's last
    reported generation differs from the server-authoritative desired one —
    INCLUDING the never-reported case — so an operator can SEE that a config push
    has not yet converged on the box."""

    def test_desired_reflects_operator_config(self):
        from app.routers.admin_runners import _runner_config_view

        view = _runner_config_view(
            {
                "max_concurrent": 4,
                "max_concurrent_compute": 2,
                "task_types": ["fix", "create"],
                "gpu_indices_allowed": [0, 1],
            }
        )
        assert view["desired"]["max_concurrent"] == 4
        assert view["desired"]["max_concurrent_compute"] == 2
        assert view["desired"]["task_types"] == ["fix", "create"]
        assert view["desired"]["gpu_indices_allowed"] == [0, 1]
        # Surfaced at the top level for the panel's GPU chip too.
        assert view["gpu_indices_allowed"] == [0, 1]

    def test_pending_when_daemon_never_reported(self):
        from app.routers.admin_runners import _runner_config_view

        # No applied_config_generation on caps → the daemon has not confirmed it
        # applied anything → pending, never falsely "applied".
        view = _runner_config_view({"max_concurrent": 2})
        assert view["applied_config_generation"] is None
        assert view["config_pending"] is True

    def test_not_pending_once_daemon_reports_matching_generation(self):
        from app.routers.admin_runners import _runner_config_view
        from app.runner_config import live_safe_config_generation

        caps = {"max_concurrent": 2, "gpu_indices_allowed": [3]}
        # Simulate the daemon echoing back the generation it now has in effect.
        caps["applied_config_generation"] = live_safe_config_generation(caps)
        view = _runner_config_view(caps)
        assert view["config_pending"] is False
        assert view["applied_config_generation"] == view["config_generation"]

    def test_pending_when_reported_generation_is_stale(self):
        from app.routers.admin_runners import _runner_config_view

        # Daemon reported an OLD generation that no longer matches desired.
        view = _runner_config_view(
            {"max_concurrent": 8, "applied_config_generation": "stale-generation"}
        )
        assert view["config_pending"] is True

    def test_gpu_allowlist_change_flips_pending(self):
        from app.routers.admin_runners import _runner_config_view
        from app.runner_config import live_safe_config_generation

        # Daemon has converged on the current allowlist ([0]).
        caps = {"max_concurrent": 2, "gpu_indices_allowed": [0]}
        caps["applied_config_generation"] = live_safe_config_generation(caps)
        assert _runner_config_view(caps)["config_pending"] is False

        # Operator widens the allowlist to [0, 1]: the generation changes, so the
        # previously-applied generation is now stale → pending again until the
        # daemon reports the new one. This is the exact "widen dgx-1's GPU
        # allowlist without SSH" flow the salvaged findings describe.
        caps["gpu_indices_allowed"] = [0, 1]
        assert _runner_config_view(caps)["config_pending"] is True

    def test_desired_changed_at_surfaced_from_stamp(self):
        from app.routers.admin_runners import _runner_config_view

        # The stamp written by the operator PATCH / registration baseline is
        # surfaced verbatim so the Fleet UI can bound how long a push has pended.
        view = _runner_config_view(
            {"max_concurrent": 2, "live_config_changed_at": "2026-07-14T00:00:00+00:00"}
        )
        assert view["desired_changed_at"] == "2026-07-14T00:00:00+00:00"

    def test_desired_changed_at_none_for_legacy_caps_without_stamp(self):
        from app.routers.admin_runners import _runner_config_view

        # A runner enrolled before the stamp existed has no timestamp → unknown,
        # never a hard failure (the UI keeps it optimistic, not falsely stuck).
        view = _runner_config_view({"max_concurrent": 2})
        assert view["desired_changed_at"] is None
        # A non-string legacy value is coerced to None rather than surfaced raw.
        view_bad = _runner_config_view(
            {"max_concurrent": 2, "live_config_changed_at": 1234567890}
        )
        assert view_bad["desired_changed_at"] is None

    def test_daemon_reports_applied_distinguishes_too_old_from_reported(self):
        from app.routers.admin_runners import _runner_config_view

        # Never echoed a generation → daemon too old to speak the contract.
        never = _runner_config_view({"max_concurrent": 2})
        assert never["daemon_reports_applied"] is False
        assert never["config_pending"] is True

        # Reported a STALE generation → daemon speaks the contract but has not
        # converged (rejected/stuck), a distinct case from "too old".
        stale = _runner_config_view(
            {"max_concurrent": 8, "applied_config_generation": "stale-generation"}
        )
        assert stale["daemon_reports_applied"] is True
        assert stale["config_pending"] is True

    def test_config_status_distinguishes_stuck_from_pending(self):
        """Acceptance: the serialized config block carries exactly the signals the
        Fleet UI needs to tell a genuinely-stuck push from a fresh, legitimately
        pending one — the desired-change timestamp (how long it has pended) and
        whether the daemon reports applied generations at all (too-old vs
        rejected). Both pending cases share ``config_pending: True``; the added
        fields are what make them distinguishable."""
        from app.routers.admin_runners import _runner_config_view

        # Case A — fresh pending: operator just changed config, daemon has not yet
        # reported. Stamped, and daemon_reports_applied False.
        fresh = _runner_config_view(
            {"max_concurrent": 4, "live_config_changed_at": "2026-07-14T12:00:00+00:00"}
        )
        # Case B — too-old daemon: stamped long ago, still never reports applied.
        too_old = _runner_config_view(
            {"max_concurrent": 4, "live_config_changed_at": "2026-07-14T00:00:00+00:00"}
        )
        # Case C — rejected/stuck daemon: reports a stale generation, non-converged.
        rejected = _runner_config_view(
            {
                "max_concurrent": 4,
                "live_config_changed_at": "2026-07-14T00:00:00+00:00",
                "applied_config_generation": "stale-generation",
            }
        )

        # All three are pending — config_pending ALONE cannot separate them.
        assert fresh["config_pending"] is True
        assert too_old["config_pending"] is True
        assert rejected["config_pending"] is True

        # The new fields DO separate them:
        #  - every case surfaces WHEN the desired config last changed (bound the window)
        assert fresh["desired_changed_at"] == "2026-07-14T12:00:00+00:00"
        assert too_old["desired_changed_at"] == "2026-07-14T00:00:00+00:00"
        #  - too-old vs rejected split on whether the daemon reports applied at all
        assert too_old["daemon_reports_applied"] is False
        assert rejected["daemon_reports_applied"] is True

    def test_runner_config_reports_rejected_reason(self):
        """``_runner_config_view`` surfaces the daemon's structured apply-rejection
        reason (and the offending field list) ONLY when the rejected generation
        matches the currently-desired generation; a reason tied to a superseded
        generation is stale and suppressed to None (the operator has since changed
        the desired config)."""
        from app.routers.admin_runners import _runner_config_view
        from app.runner_config import live_safe_config_generation

        # Case A — rejection tied to the CURRENT desired generation → live reason.
        caps_live = {"max_concurrent": 4}
        desired_gen = live_safe_config_generation(caps_live)
        caps_live.update(
            {
                "config_apply_status": "rejected",
                "config_apply_reason": "unrecognized config field(s): foo",
                "config_rejected_fields": ["foo"],
                "config_rejected_generation": desired_gen,
            }
        )
        view_live = _runner_config_view(caps_live)
        assert view_live["apply_status"] == "rejected"
        assert view_live["apply_reason"] == "unrecognized config field(s): foo"
        assert view_live["config_rejected_fields"] == ["foo"]
        assert view_live["rejected_config_generation"] == desired_gen

        # Case B — rejection tied to a SUPERSEDED generation → reason suppressed.
        # The operator has since changed the desired config, so the daemon's stale
        # rejection (against an old generation) must NOT read as the current failure.
        caps_stale = {
            "max_concurrent": 8,  # different desired subset → different generation
            "config_apply_status": "rejected",
            "config_apply_reason": "unrecognized config field(s): foo",
            "config_rejected_fields": ["foo"],
            "config_rejected_generation": "some-old-generation",
        }
        view_stale = _runner_config_view(caps_stale)
        # The raw daemon-reported status/generation still pass through additively…
        assert view_stale["apply_status"] == "rejected"
        assert view_stale["rejected_config_generation"] == "some-old-generation"
        # …but the REASON and field list are suppressed (tied to a stale generation).
        assert view_stale["apply_reason"] is None
        assert view_stale["config_rejected_fields"] is None

        # Case C — legacy daemon reports none of the structured fields → all None.
        view_legacy = _runner_config_view({"max_concurrent": 2})
        assert view_legacy["apply_status"] is None
        assert view_legacy["apply_reason"] is None
        assert view_legacy["config_rejected_fields"] is None
        assert view_legacy["rejected_config_generation"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Per-job GPU count as a LIVE-SAFE, operator-configurable runner-config knob.
#
# gpus_per_job completes the operator's enumerated live-config list: it is pushed
# from the orchestrator via the heartbeat channel like gpu_indices_allowed, so an
# operator can retune how many GPUs each compute job reserves on a mixed
# multi-GPU box without SSHing in to edit an env var and restart the daemon.
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeGpusPerJob:
    def test_accepts_bounds_and_coerces_numeric_strings(self):
        assert _normalize_gpus_per_job(GPUS_PER_JOB_MIN) == GPUS_PER_JOB_MIN
        assert _normalize_gpus_per_job(GPUS_PER_JOB_MAX) == GPUS_PER_JOB_MAX
        assert _normalize_gpus_per_job(2) == 2
        # A whole-valued float and a decimal string are both accepted.
        assert _normalize_gpus_per_job(4.0) == 4
        assert _normalize_gpus_per_job("3") == 3

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            _normalize_gpus_per_job(0)
        with pytest.raises(ValueError):
            _normalize_gpus_per_job(GPUS_PER_JOB_MAX + 1)
        with pytest.raises(ValueError):
            _normalize_gpus_per_job(-1)

    def test_rejects_bool_and_junk(self):
        # A bool is an int subclass — must not silently coerce to 1.
        for bad in (True, False, "two", 1.5, None, ["1"], {}):
            with pytest.raises(ValueError):
                _normalize_gpus_per_job(bad)


class TestGpusPerJobProjection:
    def test_absent_defaults_to_min_and_present_projects_through(self):
        # Backward compatible: a runner enrolled before this field existed
        # projects the universal default rather than dropping the key.
        assert live_safe_config({})["gpus_per_job"] == GPUS_PER_JOB_MIN
        assert live_safe_config({"gpus_per_job": 4})["gpus_per_job"] == 4

    def test_invalid_or_out_of_range_stored_value_is_clamped_not_raised(self):
        # The generation hash must never raise on a legacy/oddly-shaped value.
        assert live_safe_config({"gpus_per_job": "junk"})["gpus_per_job"] == GPUS_PER_JOB_MIN
        assert live_safe_config({"gpus_per_job": 0})["gpus_per_job"] == GPUS_PER_JOB_MIN
        assert (
            live_safe_config({"gpus_per_job": GPUS_PER_JOB_MAX + 5})["gpus_per_job"]
            == GPUS_PER_JOB_MAX
        )

    def test_generation_is_sensitive_to_gpus_per_job(self):
        base = live_safe_config({"gpus_per_job": 1})
        changed = live_safe_config({"gpus_per_job": 2})
        assert config_generation(base) != config_generation(changed)


class TestGpusPerJobPatchEndpoint:
    @pytest.mark.asyncio
    async def test_patch_pushes_gpus_per_job_into_desired_config(
        self, client: AsyncClient, db_session
    ):
        """An operator PATCH lands gpus_per_job in capabilities AND in the
        desired live-safe config the daemon converges to (moving the generation)."""
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id,
            token_hash="e" * 64,
            hostname="gpu-box",
            status="active",
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        before = _runner_config_view(runner.capabilities)
        # Default projection before any operator input.
        assert before["desired"]["gpus_per_job"] == GPUS_PER_JOB_MIN
        assert before["gpus_per_job"] == GPUS_PER_JOB_MIN

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"gpus_per_job": 4},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["capabilities"]["gpus_per_job"] == 4
        # The desired live-safe config surfaced to the Fleet UI reflects the push
        # and the generation advanced, so the daemon sees config_pending.
        cfg = body["config"]
        assert cfg["desired"]["gpus_per_job"] == 4
        assert cfg["gpus_per_job"] == 4
        assert cfg["config_generation"] != before["config_generation"]

        await db_session.refresh(runner)
        assert runner.capabilities["gpus_per_job"] == 4
        # Orthogonal to other operator fields — max_concurrent is untouched.
        assert runner.capabilities["max_concurrent"] == 2

    @pytest.mark.asyncio
    async def test_patch_rejects_out_of_range_gpus_per_job(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="f" * 64, hostname="gpu-box2", status="active"
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        for bad in (0, GPUS_PER_JOB_MAX + 1, -3):
            rej = await client.patch(
                f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
                json={"gpus_per_job": bad},
            )
            assert rej.status_code == 422, bad


# ═══════════════════════════════════════════════════════════════════════════════
# Desired-config-changed stamp: the clock the Fleet UI uses to bound how long a
# config push has been pending (fresh-pending vs genuinely-stuck).
# ═══════════════════════════════════════════════════════════════════════════════


class TestLiveConfigChangedStamp:
    @pytest.mark.asyncio
    async def test_register_stamps_baseline(self, client: AsyncClient, db_session):
        """A freshly-registered runner carries a baseline desired-config-changed
        stamp so its first (never-reported) pending state can be bounded from
        enrollment rather than reading "just changed" forever."""
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/register",
            json={"hostname": "fresh-box", "capabilities": {"max_concurrent": 2}},
        )
        assert resp.status_code == 200, resp.text

        # The list view surfaces the baseline stamp as desired_changed_at so the
        # first (never-reported) pending state can be bounded from enrollment.
        listing = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert listing.status_code == 200, listing.text
        rows = listing.json()
        assert len(rows) == 1
        cfg = rows[0]["config"]
        assert isinstance(cfg["desired_changed_at"], str)
        assert cfg["daemon_reports_applied"] is False
        assert cfg["config_pending"] is True

    @pytest.mark.asyncio
    async def test_live_safe_patch_restamps_but_noop_patch_does_not(
        self, client: AsyncClient, db_session
    ):
        """A PATCH that actually moves the live-safe generation re-stamps the
        desired-config-changed clock; a no-op PATCH (same value re-submitted)
        leaves the existing stamp untouched — otherwise the STUCK timeout could
        be reset forever by idempotent re-saves and never fire."""
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id,
            token_hash="a" * 64,
            hostname="stamp-box",
            status="active",
            capabilities={
                "task_types": "all",
                "max_concurrent": 2,
                "live_config_changed_at": "2020-01-01T00:00:00+00:00",
            },
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        # Real change: max_concurrent 2 → 4 moves the generation → re-stamp.
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 4},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(runner)
        restamped = runner.capabilities["live_config_changed_at"]
        assert restamped != "2020-01-01T00:00:00+00:00"

        # No-op: re-submit the SAME value → generation unchanged → stamp preserved.
        resp2 = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 4},
        )
        assert resp2.status_code == 200, resp2.text
        await db_session.refresh(runner)
        assert runner.capabilities["live_config_changed_at"] == restamped


class TestRunnerMemoryTelemetryContract:
    """Cross-system contract lock for memory/CPU health on the fleet read path.

    The runner heartbeat reports ``memory_free_bytes`` / ``cpu_load_avg`` (see
    scripts/local_runner.py), the ingest stashes them on ``capabilities``, and
    ``_runner_health`` derives the ``memory_low`` flag alongside the disk flag.
    The operator surfaces (FleetHealthCard, FleetRunnersPanel) render these to
    warn on a memory-starved runner. Disk was wired end-to-end; memory/CPU were
    computed by the backend but silently dropped by the admin-ui contract, so a
    memory-starved fleet read as healthy. These tests lock the backend read path
    so a future change can never again drop the fields the operator UI depends
    on. Threshold: ``_LOW_MEMORY_FREE_BYTES`` = 512 MiB.
    """

    async def _make_live_runner(self, session, project_id, *, capabilities, hostname):
        runner = LocalRunner(
            project_id=project_id,
            token_hash="a" * 64,
            hostname=hostname,
            status="active",
            last_seen_at=_utcnow() - timedelta(seconds=15),
            capabilities=capabilities,
        )
        session.add(runner)
        await session.commit()
        await session.refresh(runner)
        return runner

    @pytest.mark.asyncio
    async def test_list_runners_health_emits_memory_and_cpu_and_flags_low_memory(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        low_mem_bytes = 200 * 1024**2  # 200 MiB < 512 MiB threshold
        runner = await self._make_live_runner(
            db_session,
            project.id,
            hostname="mem-starved",
            capabilities={
                "task_types": "all",
                "max_concurrent": 2,
                # Disk is healthy; only memory is starved — proves the two flags
                # are independent and the low-memory case is what trips.
                "disk_free_bytes": 50 * 1024**3,
                "memory_free_bytes": low_mem_bytes,
                "cpu_load_avg": 3.5,
                "gpu_utilization_pct": 10,
            },
        )

        resp = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert resp.status_code == 200, resp.text
        health = {r["id"]: r["health"] for r in resp.json()}[str(runner.id)]

        assert health["device_status"] == "connected"
        assert health["memory_free_bytes"] == low_mem_bytes
        assert health["memory_low"] is True
        assert health["cpu_load_avg"] == pytest.approx(3.5)
        # Disk stays healthy — the memory flag must not be conflated with disk.
        assert health["disk_low"] is False

    @pytest.mark.asyncio
    async def test_list_runners_health_omits_low_memory_for_healthy_runner(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner = await self._make_live_runner(
            db_session,
            project.id,
            hostname="mem-healthy",
            capabilities={
                "task_types": "all",
                "max_concurrent": 2,
                "memory_free_bytes": 8 * 1024**3,  # 8 GiB, well above threshold
                "cpu_load_avg": 0.25,
            },
        )

        resp = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert resp.status_code == 200, resp.text
        health = {r["id"]: r["health"] for r in resp.json()}[str(runner.id)]

        assert health["memory_low"] is False
        assert health["memory_free_bytes"] == 8 * 1024**3
        assert health["cpu_load_avg"] == pytest.approx(0.25)

    @pytest.mark.asyncio
    async def test_fleet_health_summary_counts_low_memory(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        await self._make_live_runner(
            db_session,
            project.id,
            hostname="mem-starved",
            capabilities={
                "task_types": "all",
                "max_concurrent": 2,
                "memory_free_bytes": 100 * 1024**2,  # 100 MiB < threshold
                "cpu_load_avg": 4.0,
            },
        )

        resp = await client.get("/api/admin/fleet/runners/health-summary")
        assert resp.status_code == 200, resp.text
        summary = resp.json()

        # Fleet-wide low_memory count is emitted and reflects the starved runner.
        assert summary["low_memory"] >= 1
        entry = {e["project_id"]: e for e in summary["per_project"]}[str(project.id)]
        assert entry["low_memory"] >= 1
        # The per-runner health inside the summary carries the flag the UI renders.
        assert any(r["health"]["memory_low"] for r in entry["runners"])


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-end runner-enroll walkthrough
#
# Chains the server side of the enroll flow in one scenario:
#   register (token minted) → heartbeat (runner goes live) → rotate token →
#   revoke (runner deleted), asserting each endpoint's success shape and that
#   the runner's state is visible in the list endpoint between steps.
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunnerEnrollWalkthrough:
    """Server-side walkthrough of the enroll flow, step by step in one scenario."""

    @pytest.mark.asyncio
    async def test_enroll_walkthrough_register_heartbeat_rotate_revoke(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, "Enroll Walkthrough Project")

        # ── Step 1: register — token minted ──────────────────────────────────
        reg_resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/register",
            json={"hostname": "walkthrough-box"},
        )
        assert reg_resp.status_code == 200, reg_resp.text
        reg = reg_resp.json()
        assert "runner_id" in reg
        assert "token" in reg
        runner_id = reg["runner_id"]
        token = reg["token"]
        assert token  # one-time token must be non-empty

        # List endpoint shows the newly registered runner (not yet live).
        list_resp = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert list_resp.status_code == 200, list_resp.text
        rows = list_resp.json()
        assert any(r["id"] == runner_id for r in rows), "runner not found in list after register"
        row = next(r for r in rows if r["id"] == runner_id)
        assert row["hostname"] == "walkthrough-box"
        assert row["status"] == "active"
        # Runner has not heartbeated yet — last_seen_at is null.
        assert row["last_seen_at"] is None

        # ── Step 2: heartbeat — runner appears live ───────────────────────────
        hb_resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": [], "active_work_report": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert hb_resp.status_code == 200, hb_resp.text

        # List endpoint shows the runner is now live (last_seen_at set).
        list_resp2 = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert list_resp2.status_code == 200, list_resp2.text
        rows2 = list_resp2.json()
        row2 = next(r for r in rows2 if r["id"] == runner_id)
        assert row2["last_seen_at"] is not None, "last_seen_at must be set after heartbeat"
        # Health should now show the runner as connected (recent heartbeat).
        health = row2.get("health") or {}
        assert health.get("device_status") == "connected"

        # ── Step 3: rotate token — old token invalidated, new token minted ───
        rot_resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/{runner_id}/rotate-token",
        )
        assert rot_resp.status_code == 200, rot_resp.text
        rot = rot_resp.json()
        assert rot["runner_id"] == runner_id
        assert "token" in rot
        new_token = rot["token"]
        assert new_token != token, "rotated token must differ from the original"

        # Old token is now invalid — heartbeat with it must return 401/403.
        old_hb = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert old_hb.status_code in (401, 403), (
            f"old token should be rejected after rotation, got {old_hb.status_code}"
        )

        # New token still authenticates successfully.
        new_hb = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": [], "active_work_report": True},
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert new_hb.status_code == 200, new_hb.text

        # ── Step 4: revoke — runner is deleted and absent from active list ────
        del_resp = await client.delete(
            f"/api/admin/projects/{project.id}/runners/{runner_id}",
        )
        assert del_resp.status_code == 200, del_resp.text

        # After revocation the runner no longer appears in the active list.
        list_resp3 = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert list_resp3.status_code == 200, list_resp3.text
        rows3 = list_resp3.json()
        active_ids = [r["id"] for r in rows3]
        assert runner_id not in active_ids, "revoked runner must not appear in active list"

        # The revoked runner's token can no longer authenticate.
        revoked_hb = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert revoked_hb.status_code in (401, 403), (
            f"revoked runner token should be rejected, got {revoked_hb.status_code}"
        )
