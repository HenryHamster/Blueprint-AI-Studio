"""
test_compute_watchdog.py — Consolidated acceptance-proof regression suite for:
  (1) the intermediate progress-metric contract (BLUEPRINT_PROGRESS_FILE JSONL),
  (2) the no-progress watchdog that cuts long-stalled compute jobs early.

Seams exercised:
  • create_compute_job: clamping + stamping of declared_metric /
    no_progress_timeout_seconds into polling_metadata
  • append_progress_sample: ring-buffer, validation, immutability
  • _no_progress_stalled: pure predicate (fail-open, grace period, min-samples,
    window requirement, disabled-when-zero)
  • _build_container_run_prefix / _build_local_wrapper_command: progress env injection
  • _parse_progress_record / _ProgressTail: runner-side JSONL reader
  • process_compute_jobs_once: end-to-end watchdog kill
    (terminal_kind == NO_PROGRESS_TERMINAL_REASON, distinct from deadline)
"""

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import compute_jobs
from app.compute_backends import (
    PROGRESS_FILE_ENV,
    RUNNER_SUBPROCESS_BACKEND_NAME,
    _build_container_run_prefix,
    _build_local_wrapper_command,
    ComputeCostResult,
    ComputeStatusResult,
)
from app.compute_jobs import (
    NO_PROGRESS_TERMINAL_REASON,
    COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS,
    RUNNER_SILENT_TIMEOUT_SECONDS,
    _WATCHDOG_STATUS_META_KEY,
    _apply_watchdog_metric_mismatch_marker,
    _no_progress_stalled,
    _watchdog_metric_name_mismatch,
    append_progress_sample,
)
from app.reaper_sweeps import sweep_silent_runner_compute_jobs
from app.database import resolve_async_engine
from app.models import (
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


# ---------------------------------------------------------------------------
# Helpers shared across pure-function tests
# ---------------------------------------------------------------------------

_SPAN_AGES_SECONDS = [2000, 1500, 1100, 700, 300, 0]


def _make_stub_job(*, metric, samples, started_offset_seconds, extra_meta=None):
    """Build an in-memory job stub for pure watchdog tests (no DB).

    ``samples`` is a list of ``(seconds_before_now, name, value)`` tuples;
    ``started_offset_seconds`` is how many seconds ago the job started.
    """
    now = datetime(2026, 1, 2, 12, 0, 0)
    meta = dict(extra_meta or {})
    if metric is not None:
        meta["declared_metric"] = metric
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "name": name,
            "value": value,
        }
        for (age, name, value) in samples
    ]
    job = SimpleNamespace(
        polling_metadata=meta,
        started_at=now - timedelta(seconds=started_offset_seconds),
    )
    return job, now


def _flat_samples(metric, value=1.0):
    return [(age, metric, value) for age in _SPAN_AGES_SECONDS]


def _advancing_samples(metric):
    return [(age, metric, 3.0 - i * 0.2) for i, age in enumerate(_SPAN_AGES_SECONDS)]


# ---------------------------------------------------------------------------
# ── PROGRESS-METRIC CONTRACT: env constant ──────────────────────────────────
# ---------------------------------------------------------------------------


def test_progress_file_env_constant_value():
    assert PROGRESS_FILE_ENV == "BLUEPRINT_PROGRESS_FILE"


# ---------------------------------------------------------------------------
# ── PROGRESS-METRIC CONTRACT: append_progress_sample ───────────────────────
# ---------------------------------------------------------------------------


def test_append_progress_sample_roundtrips_valid_entry():
    meta = {}
    result = append_progress_sample(meta, metric="loss", value=0.42, t="2026-01-01T00:00:00")
    series = result["progress_series"]
    assert len(series) == 1
    entry = series[0]
    assert entry["name"] == "loss"
    assert entry["value"] == pytest.approx(0.42)
    assert entry["t"] == "2026-01-01T00:00:00"


def test_append_progress_sample_drops_nonfinite_values():
    for bad in (float("inf"), float("-inf"), float("nan")):
        result = append_progress_sample({}, metric="loss", value=bad, t="t0")
        assert result == {}, f"expected no mutation for value={bad!r}"


def test_append_progress_sample_drops_bad_metric_key():
    # Space in key → rejected; result unchanged
    assert append_progress_sample({}, metric="bad key", value=1.0, t="t0") == {}
    # Exclamation mark → rejected
    assert append_progress_sample({}, metric="loss!", value=1.0, t="t0") == {}


def test_append_progress_sample_history_is_bounded_per_metric():
    meta: dict = {}
    for i in range(compute_jobs._PROGRESS_SERIES_MAX_SAMPLES_PER_METRIC + 5):
        meta = append_progress_sample(meta, metric="loss", value=float(i), t=f"t{i}")
    series = meta["progress_series"]
    assert len(series) == compute_jobs._PROGRESS_SERIES_MAX_SAMPLES_PER_METRIC
    assert series[0]["value"] == pytest.approx(5.0), "oldest 5 samples dropped"
    assert series[-1]["value"] == pytest.approx(
        float(compute_jobs._PROGRESS_SERIES_MAX_SAMPLES_PER_METRIC + 4)
    )


def test_append_progress_sample_does_not_mutate_input():
    original = {"other": "data"}
    result = append_progress_sample(original, metric="loss", value=0.1, t="t0")
    assert "progress_series" not in original
    assert "progress_series" in result
    assert original["other"] == "data"


# ---------------------------------------------------------------------------
# ── PROGRESS-METRIC CONTRACT: backend env injection ─────────────────────────
# ---------------------------------------------------------------------------


def test_build_container_run_prefix_injects_progress_file_env():
    output = _build_container_run_prefix("myimage:latest", host_out_dir="/tmp/bpout")
    assert PROGRESS_FILE_ENV in output
    assert "BLUEPRINT_PROGRESS_FILE" in output


def test_build_local_wrapper_command_injects_progress_file_env():
    script = _build_local_wrapper_command(
        "python train.py",
        external_id="test-eid-abc123",
    )
    assert "BLUEPRINT_PROGRESS_FILE" in script
    assert PROGRESS_FILE_ENV in script


# ---------------------------------------------------------------------------
# ── PROGRESS-METRIC CONTRACT: runner-side (_parse_progress_record /
#    _ProgressTail) via the scripts/ tree ────────────────────────────────────
# ---------------------------------------------------------------------------


def _resolve_scripts_dir():
    candidates = []
    env_override = os.getenv("BLUEPRINT_SCRIPTS_DIR")
    if env_override:
        candidates.append(env_override)
    candidates.append("/repo/scripts")
    candidates.append(
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
    )
    for path in candidates:
        if os.path.isfile(os.path.join(path, "local_runner.py")):
            return path
    return None


def _import_local_runner():
    scripts_dir = _resolve_scripts_dir()
    if scripts_dir is None:
        msg = (
            "scripts/local_runner.py not available "
            "(checked BLUEPRINT_SCRIPTS_DIR, /repo/scripts, ../../scripts)"
        )
        if os.getenv("CI"):
            pytest.fail(msg + " — refusing to skip in CI")
        pytest.skip(msg)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import local_runner as lr
    except Exception as exc:
        if os.getenv("CI"):
            pytest.fail(f"local_runner unimportable in CI: {exc}")
        pytest.skip(f"local_runner unimportable: {exc}")
    return lr


class TestParseProgressRecord:
    def test_valid_line_returns_dict_with_metric_and_value(self):
        lr = _import_local_runner()
        result = lr._parse_progress_record(b'{"metric": "loss", "value": 1.5}')
        assert result is not None
        assert result["metric"] == "loss"
        assert result["value"] == pytest.approx(1.5)

    def test_valid_line_with_optional_step_and_ts(self):
        lr = _import_local_runner()
        result = lr._parse_progress_record(
            b'{"metric": "acc", "value": 0.9, "step": 10, "ts": "t1"}'
        )
        assert result is not None
        assert result["step"] == 10
        assert result["ts"] == "t1"

    def test_rejects_nonfinite_value(self):
        lr = _import_local_runner()
        assert lr._parse_progress_record(b'{"metric": "loss", "value": Infinity}') is None
        assert lr._parse_progress_record(b'{"metric": "loss", "value": NaN}') is None

    def test_rejects_missing_metric(self):
        lr = _import_local_runner()
        assert lr._parse_progress_record(b'{"value": 1.0}') is None

    def test_rejects_empty_metric(self):
        lr = _import_local_runner()
        assert lr._parse_progress_record(b'{"metric": "", "value": 1.0}') is None

    def test_rejects_oversized_line(self):
        lr = _import_local_runner()
        oversized = b'{"metric": "loss", "value": 1.0, "pad": "' + b"A" * 9000 + b'"}'
        assert lr._parse_progress_record(oversized) is None

    def test_rejects_non_dict_json(self):
        lr = _import_local_runner()
        assert lr._parse_progress_record(b"[1, 2, 3]") is None
        assert lr._parse_progress_record(b'"just a string"') is None

    def test_rejects_non_numeric_value(self):
        lr = _import_local_runner()
        assert lr._parse_progress_record(b'{"metric": "loss", "value": "high"}') is None
        assert lr._parse_progress_record(b'{"metric": "loss", "value": true}') is None


class TestProgressTail:
    def test_offset_tracking_across_polls(self, tmp_path):
        lr = _import_local_runner()
        path = tmp_path / "progress.jsonl"
        tail = lr._ProgressTail(str(path))

        assert tail.poll() is None  # file absent → no error

        with open(path, "a", encoding="utf-8") as f:
            f.write('{"metric": "loss", "value": 3.0, "step": 1}\n')
            f.write('{"metric": "loss", "value": 2.5, "step": 2}\n')
        snap = tail.poll()
        assert snap["count"] == 2

        # Only newly appended bytes are consumed on the next poll
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"metric": "loss", "value": 2.0, "step": 3}\n')
        snap = tail.poll()
        assert snap["count"] == 3

    def test_partial_line_carried_between_polls(self, tmp_path):
        lr = _import_local_runner()
        path = tmp_path / "progress.jsonl"
        tail = lr._ProgressTail(str(path))
        line = '{"metric": "loss", "value": 3.0, "step": 1}\n'
        with open(path, "a", encoding="utf-8") as f:
            f.write(line[:20])
        assert tail.poll() is None
        with open(path, "a", encoding="utf-8") as f:
            f.write(line[20:])
        snap = tail.poll()
        assert snap is not None
        assert snap["count"] == 1

    def test_resets_on_file_truncation(self, tmp_path):
        lr = _import_local_runner()
        path = tmp_path / "progress.jsonl"
        tail = lr._ProgressTail(str(path))
        # Write two lines so the tracked offset advances well past the new-file size
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"metric": "loss", "value": 3.0, "step": 1}\n')
            f.write('{"metric": "loss", "value": 2.5, "step": 2}\n')
        assert tail.poll()["count"] == 2
        # Rewrite with SHORTER content — triggers size < offset → reader resets
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"metric": "loss", "value": 2.0, "step": 3}\n')
        snap = tail.poll()
        assert snap["count"] == 3, "reader must re-read from offset 0 after truncation"
        assert snap["records"][-1]["step"] == 3


# ---------------------------------------------------------------------------
# ── NO-PROGRESS CUT: pure _no_progress_stalled predicate ────────────────────
# ---------------------------------------------------------------------------


def test_no_progress_stalled_true_on_flat_metric_past_window():
    metric = "val/loss"
    job, now = _make_stub_job(
        metric=metric,
        samples=_flat_samples(metric),
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _no_progress_stalled(job, now=now) is True


def test_no_progress_stalled_false_on_advancing_metric():
    metric = "val/loss"
    job, now = _make_stub_job(
        metric=metric,
        samples=_advancing_samples(metric),
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_false_without_declared_metric():
    metric = "val/loss"
    job, now = _make_stub_job(
        metric=None,  # no declared_metric key
        samples=_flat_samples(metric),
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_true_when_declared_metric_never_arrives():
    now = datetime(2026, 1, 2, 12, 0, 0)
    job, _ = _make_stub_job(
        metric="loss", samples=[], started_offset_seconds=4000,
        extra_meta={"no_progress_timeout_seconds": 300},
    )
    assert _no_progress_stalled(job, now=now) is True


def test_no_progress_stalled_false_during_grace_period():
    metric = "val/loss"
    # Job only started 10 minutes ago (well inside the 30-min grace)
    job, now = _make_stub_job(
        metric=metric,
        samples=_flat_samples(metric),
        started_offset_seconds=600,
    )
    assert _no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_false_with_insufficient_samples():
    metric = "val/loss"
    # Only 1 flat sample — below MIN_SAMPLES floor
    job, now = _make_stub_job(
        metric=metric,
        samples=[(0, metric, 1.0)],
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_false_when_window_not_yet_spanned():
    metric = "val/loss"
    # Samples bunched into last ~2 minutes — not a full window of history
    samples = [(120 - i * 20, metric, 1.0) for i in range(6)]
    job, now = _make_stub_job(
        metric=metric,
        samples=samples,
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_disabled_when_window_seconds_zero(monkeypatch):
    monkeypatch.setattr(compute_jobs, "COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS", 0.0)
    metric = "val/loss"
    job, now = _make_stub_job(
        metric=metric,
        samples=_flat_samples(metric),
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_terminal_reason_constant_distinct_from_deadline():
    assert NO_PROGRESS_TERMINAL_REASON == "no_progress"
    assert NO_PROGRESS_TERMINAL_REASON != "deadline"


# ---------------------------------------------------------------------------
# ── WATCHDOG-INERT DETECTION: _watchdog_metric_name_mismatch predicate ──────
# The watchdog keys on EXACT metric-name equality, so a job that reports
# progress only under a name other than its declared metric leaves the watchdog
# silently inert. These prove the poller can surface that instead of failing
# silent.
# ---------------------------------------------------------------------------


def test_metric_mismatch_flags_progress_under_other_name():
    # Declares "val/loss" but every sample is reported under "train/loss".
    job, _now = _make_stub_job(
        metric="val/loss",
        samples=_flat_samples("train/loss"),
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _watchdog_metric_name_mismatch(job) == ["train/loss"]


def test_metric_mismatch_empty_when_declared_metric_is_reported():
    job, _now = _make_stub_job(
        metric="val/loss",
        samples=_flat_samples("val/loss"),
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _watchdog_metric_name_mismatch(job) == []


def test_metric_mismatch_empty_when_declared_metric_present_among_others():
    # Declared metric IS reported (alongside others) → watchdog is live, no flag.
    samples = [
        (2000, "train/loss", 1.0),
        (1500, "val/loss", 1.0),
        (1000, "train/loss", 1.0),
    ]
    job, _now = _make_stub_job(
        metric="val/loss",
        samples=samples,
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _watchdog_metric_name_mismatch(job) == []


def test_metric_mismatch_empty_without_declared_metric():
    job, _now = _make_stub_job(
        metric=None,
        samples=_flat_samples("train/loss"),
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _watchdog_metric_name_mismatch(job) == []


def test_metric_mismatch_empty_when_no_samples_at_all():
    # A genuinely silent job (no telemetry) is the watchdog's fail-open domain,
    # NOT a name mismatch — it must not be flagged.
    job, _now = _make_stub_job(
        metric="val/loss",
        samples=[],
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _watchdog_metric_name_mismatch(job) == []


def test_metric_mismatch_dedupes_and_sorts_observed_names():
    samples = [
        (2000, "z_metric", 1.0),
        (1500, "a_metric", 1.0),
        (1000, "z_metric", 2.0),
    ]
    job, _now = _make_stub_job(
        metric="val/loss",
        samples=samples,
        started_offset_seconds=int(COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS) + 3600,
    )
    assert _watchdog_metric_name_mismatch(job) == ["a_metric", "z_metric"]


def test_apply_mismatch_marker_stamps_and_clears():
    job = SimpleNamespace(polling_metadata={"declared_metric": "val/loss"})
    _apply_watchdog_metric_mismatch_marker(job, ["train/loss"])
    marker = job.polling_metadata[_WATCHDOG_STATUS_META_KEY]
    assert marker["state"] == "inert_metric_name_mismatch"
    assert marker["declared_metric"] == "val/loss"
    assert marker["observed_metrics"] == ["train/loss"]
    # A later poll where the declared metric is finally reported clears the marker.
    _apply_watchdog_metric_mismatch_marker(job, [])
    assert _WATCHDOG_STATUS_META_KEY not in job.polling_metadata


# ---------------------------------------------------------------------------
# ── PROGRESS-METRIC CONTRACT: create_compute_job parameter validation ────────
# DB-backed: use the same db_session fixture as test_compute.py
# ---------------------------------------------------------------------------


async def _setup_task(db_session):
    project = Project(name="Watchdog Test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_create_job_stamps_declared_metric_in_polling_metadata(db_session):
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    assert job.polling_metadata["declared_metric"] == "val/loss"


@pytest.mark.asyncio
async def test_create_job_strips_whitespace_from_declared_metric(db_session):
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="  reward  ",
    )
    assert job.polling_metadata["declared_metric"] == "reward"


@pytest.mark.asyncio
async def test_create_job_stamps_clamped_no_progress_timeout(db_session):
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        no_progress_timeout_seconds=3600,
    )
    stored = job.polling_metadata.get("no_progress_timeout_seconds")
    assert stored == 3600


@pytest.mark.asyncio
async def test_create_job_clamps_small_no_progress_timeout_to_60s(db_session):
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        no_progress_timeout_seconds=10,  # below the 60s floor
    )
    stored = job.polling_metadata.get("no_progress_timeout_seconds")
    assert stored == 60, "values below 60s are clamped up to 60"


@pytest.mark.asyncio
async def test_create_job_zero_no_progress_timeout_disables_watchdog(db_session):
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        no_progress_timeout_seconds=0,
    )
    assert "no_progress_timeout_seconds" not in (job.polling_metadata or {})


@pytest.mark.asyncio
async def test_create_job_negative_no_progress_timeout_raises(db_session):
    task = await _setup_task(db_session)
    with pytest.raises(ValueError, match="no_progress_timeout_seconds must be positive"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            spend_cap_usd=1.0,
            deadline_seconds=86400,
            no_progress_timeout_seconds=-1,
        )


# ---------------------------------------------------------------------------
# ── NO-PROGRESS CUT: monitor-loop end-to-end kill ───────────────────────────
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_once_kills_stalled_job_with_no_progress_terminal_kind(
    db_session, monkeypatch
):
    """process_compute_jobs_once transitions a flat-metric job to TIMED_OUT
    with terminal_kind='no_progress', distinct from a wall-clock 'deadline' kill."""
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    now = _utcnow()
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    meta["runtime_deadline_at"] = (now + timedelta(hours=20)).isoformat()
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "name": "val/loss",
            "value": 2.71828,  # flat
        }
        for age in _SPAN_AGES_SECONDS
    ]
    job.polling_metadata = meta
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "stalled-job-xyz"
    job.started_at = now - timedelta(hours=5)
    await db_session.commit()

    terminate_reasons: list[str] = []

    class _FakeBackend:
        def external_id_for(self, j):
            return "stalled-job-xyz"

        async def poll_cost(self, j, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, j):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, j, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _: _FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert terminate_reasons == ["no_progress"]
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert job.polling_metadata.get("terminal_kind") == NO_PROGRESS_TERMINAL_REASON
    assert "no measurable progress" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_process_once_does_not_kill_advancing_metric_job(db_session, monkeypatch):
    """A job whose declared metric is still advancing must not be terminated."""
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    now = _utcnow()
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    meta["runtime_deadline_at"] = (now + timedelta(hours=20)).isoformat()
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "name": "val/loss",
            "value": 3.0 - i * 0.15,  # steadily improving
        }
        for i, age in enumerate(_SPAN_AGES_SECONDS)
    ]
    job.polling_metadata = meta
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "advancing-job-xyz"
    job.started_at = now - timedelta(hours=5)
    await db_session.commit()

    class _FakeBackend:
        def external_id_for(self, j):
            return "advancing-job-xyz"

        async def poll_cost(self, j, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, j):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, j, *, reason):
            raise AssertionError(f"advancing job must not be terminated (reason={reason!r})")

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _: _FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING


@pytest.mark.asyncio
async def test_process_once_surfaces_metric_name_mismatch_without_killing(
    db_session, monkeypatch
):
    """A running job that reports progress ONLY under a name other than its
    declared metric leaves the watchdog inert. The poller must NOT kill it, but
    MUST stamp the honest inert-watchdog marker so the mismatch is observable."""
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    now = _utcnow()
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    meta["runtime_deadline_at"] = (now + timedelta(hours=20)).isoformat()
    # Steady progress, but every sample is under "train/loss", NOT the declared
    # "val/loss" — the watchdog would never accumulate a single sample to act on.
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "name": "train/loss",
            "value": 2.71828,
        }
        for age in _SPAN_AGES_SECONDS
    ]
    job.polling_metadata = meta
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "mismatch-job-xyz"
    job.started_at = now - timedelta(hours=5)
    await db_session.commit()

    class _FakeBackend:
        def external_id_for(self, j):
            return "mismatch-job-xyz"

        async def poll_cost(self, j, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, j):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, j, *, reason):
            raise AssertionError(
                f"name-mismatch job must not be terminated (reason={reason!r})"
            )

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _: _FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING
    marker = (job.polling_metadata or {}).get(_WATCHDOG_STATUS_META_KEY)
    assert marker is not None, "inert-watchdog mismatch marker must be stamped"
    assert marker["state"] == "inert_metric_name_mismatch"
    assert marker["declared_metric"] == "val/loss"
    assert marker["observed_metrics"] == ["train/loss"]


# ---------------------------------------------------------------------------
# ── SILENT RUNNER SWEEP (Step 22f) ───────────────────────────────────────
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_reaps_runner_job_with_stale_heartbeat(db_session):
    """A RUNNING runner-subprocess job with stale last_heartbeat_at is
    finalized as TIMED_OUT with terminal_kind 'runner_died_silently'."""
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
    )
    now = _utcnow()
    job.backend = RUNNER_SUBPROCESS_BACKEND_NAME
    job.status = ComputeJobStatus.RUNNING
    job.started_at = now - timedelta(hours=2)
    job.last_heartbeat_at = now - timedelta(seconds=RUNNER_SILENT_TIMEOUT_SECONDS + 60)
    await db_session.commit()

    finalized = await sweep_silent_runner_compute_jobs(db_session, now=now)

    assert finalized == 1
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert (job.polling_metadata or {}).get("terminal_kind") == "runner_died_silently"
    assert (job.terminal_reason or "") == "runner_died_silently"


@pytest.mark.asyncio
async def test_sweep_skips_recently_heartbeating_runner_job(db_session):
    """A RUNNING runner-subprocess job with a recent last_heartbeat_at is
    left untouched by the sweep."""
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
    )
    now = _utcnow()
    job.backend = RUNNER_SUBPROCESS_BACKEND_NAME
    job.status = ComputeJobStatus.RUNNING
    job.started_at = now - timedelta(hours=2)
    job.last_heartbeat_at = now - timedelta(seconds=60)
    await db_session.commit()

    finalized = await sweep_silent_runner_compute_jobs(db_session, now=now)

    assert finalized == 0
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING


@pytest.mark.asyncio
async def test_sweep_skips_non_runner_backends(db_session):
    """Non-runner-subprocess jobs (GCP, local) with stale heartbeats are
    not touched by the sweep."""
    task = await _setup_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
    )
    now = _utcnow()
    # Set backend to something that is NOT runner-subprocess
    job.backend = "gcp"
    job.status = ComputeJobStatus.RUNNING
    job.started_at = now - timedelta(hours=2)
    job.last_heartbeat_at = now - timedelta(seconds=RUNNER_SILENT_TIMEOUT_SECONDS + 60)
    await db_session.commit()

    finalized = await sweep_silent_runner_compute_jobs(db_session, now=now)

    assert finalized == 0
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING


@pytest.mark.asyncio
async def test_sweep_skips_non_running_runner_jobs(db_session):
    """Runner-subprocess jobs in non-RUNNING statuses (STARTING, SUCCEEDED)
    with stale heartbeats are not touched by the sweep."""
    for initial_status in (ComputeJobStatus.STARTING, ComputeJobStatus.SUCCEEDED):
        task = await _setup_task(db_session)
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            spend_cap_usd=1.0,
            deadline_seconds=86400,
        )
        now = _utcnow()
        job.backend = RUNNER_SUBPROCESS_BACKEND_NAME
        job.status = initial_status
        job.started_at = now - timedelta(hours=2)
        job.last_heartbeat_at = now - timedelta(seconds=RUNNER_SILENT_TIMEOUT_SECONDS + 60)
        await db_session.commit()

        finalized = await sweep_silent_runner_compute_jobs(db_session, now=now)

        assert finalized == 0
        await db_session.refresh(job)
        assert job.status == initial_status
