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
