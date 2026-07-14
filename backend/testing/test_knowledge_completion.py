"""Tests for knowledge_completion module — evidence_tier computation and verdict_record fields."""

from unittest.mock import MagicMock

from app.knowledge_completion import (
    _build_compute_experiment_document,
    _compute_evidence_tier,
    _extract_job_terminal_diagnostics,
)
from testing.factories import make_task


# ── _compute_evidence_tier ───────────────────────────────────────────────────


class TestComputeEvidenceTier:
    def _make_job(self, backend: str | None = None, terminal_reason: str | None = None):
        job = MagicMock()
        job.backend = backend
        job.terminal_reason = terminal_reason
        return job

    def test_no_job_no_metrics_returns_uncovered(self):
        assert _compute_evidence_tier(None, None) == "uncovered"

    def test_no_job_with_metrics_returns_backfilled(self):
        assert _compute_evidence_tier(None, {"accuracy": 0.95}) == "backfilled"

    def test_local_backend_with_metrics_returns_covered(self):
        """Ran on operator hardware AND produced a result → genuinely verified."""
        job = self._make_job(backend="local-bwrap")
        assert _compute_evidence_tier(job, {"accuracy": 0.95}) == "covered"

    def test_local_container_backend_returns_covered(self):
        job = self._make_job(backend="local-container")
        assert _compute_evidence_tier(job, {"loss": 0.1}) == "covered"

    def test_local_backend_WITHOUT_metrics_is_not_machine_verified(self):
        """A local run that measured NOTHING must not render as "Machine verified".

        ``covered`` is the tier the UI labels "Machine verified".  Deriving it from
        the backend name alone made it a LOCATION proxy: an empty run on operator
        hardware — one that crashed, or never emitted its metric — claimed
        verification it never earned.  That is the claimed-not-earned status this
        version exists to eliminate, and it contradicts the compute results
        contract (an unmeasured run is a FAILURE, not a success).

        Execution provenance is real, so it is not ``uncovered`` — it is
        ``partial``: we know WHERE it ran, we have nothing it proved.
        """
        job = self._make_job(backend="local-bwrap")
        assert _compute_evidence_tier(job, None) == "partial"
        assert _compute_evidence_tier(job, {}) == "partial"

    def test_runner_subprocess_without_metrics_is_not_machine_verified(self):
        job = self._make_job(backend="runner-subprocess")
        assert _compute_evidence_tier(job, None) == "partial"

    def test_runner_subprocess_with_metrics_returns_covered(self):
        job = self._make_job(backend="runner-subprocess")
        assert _compute_evidence_tier(job, {"throughput": 1200}) == "covered"

    def test_gcp_backend_returns_attested(self):
        job = self._make_job(backend="gcp")
        assert _compute_evidence_tier(job, None) == "attested"

    def test_cloud_backend_returns_attested(self):
        job = self._make_job(backend="cloud-gcp-n2")
        assert _compute_evidence_tier(job, None) == "attested"

    def test_no_backend_with_metrics_returns_backfilled(self):
        job = self._make_job(backend=None)
        assert _compute_evidence_tier(job, {"score": 0.8}) == "backfilled"

    def test_no_backend_no_metrics_returns_partial(self):
        job = self._make_job(backend=None)
        assert _compute_evidence_tier(job, None) == "partial"

    def test_empty_backend_with_metrics_returns_backfilled(self):
        job = self._make_job(backend="")
        assert _compute_evidence_tier(job, {"f1": 0.9}) == "backfilled"


# ── _build_compute_experiment_document — evidence_tier in verdict_record ─────


def _base_task():
    return make_task(
        payloads=[{"type": "compute_experiment", "experiment": "bench", "tags": []}]
    )


class TestBuildComputeExperimentDocumentEvidenceTier:
    def test_evidence_tier_covered_in_verdict_record(self):
        task = _base_task()
        doc = _build_compute_experiment_document(
            task,
            outcome="completed",
            cost=0.05,
            criterion=None,
            result_reference=None,
            evidence_tier="covered",
        )
        assert doc["verdict_record"]["evidence_tier"] == "covered"

    def test_evidence_tier_attested_in_verdict_record(self):
        task = _base_task()
        doc = _build_compute_experiment_document(
            task,
            outcome="completed",
            cost=0.10,
            criterion=None,
            result_reference=None,
            evidence_tier="attested",
        )
        assert doc["verdict_record"]["evidence_tier"] == "attested"

    def test_evidence_tier_uncovered_in_verdict_record(self):
        task = _base_task()
        doc = _build_compute_experiment_document(
            task,
            outcome="failed",
            cost=None,
            criterion=None,
            result_reference=None,
            evidence_tier="uncovered",
        )
        assert doc["verdict_record"]["evidence_tier"] == "uncovered"

    def test_evidence_tier_absent_when_none(self):
        task = _base_task()
        doc = _build_compute_experiment_document(
            task,
            outcome="completed",
            cost=None,
            criterion=None,
            result_reference=None,
            evidence_tier=None,
        )
        assert "evidence_tier" not in doc["verdict_record"]

    def test_evidence_tier_backfilled_in_verdict_record(self):
        task = _base_task()
        doc = _build_compute_experiment_document(
            task,
            outcome="completed",
            cost=0.02,
            criterion=None,
            result_reference=None,
            result_metrics={"accuracy": 0.97},
            evidence_tier="backfilled",
        )
        assert doc["verdict_record"]["evidence_tier"] == "backfilled"

    def test_evidence_tier_partial_in_verdict_record(self):
        task = _base_task()
        doc = _build_compute_experiment_document(
            task,
            outcome="failed",
            cost=None,
            criterion=None,
            result_reference=None,
            evidence_tier="partial",
        )
        assert doc["verdict_record"]["evidence_tier"] == "partial"


# ── _extract_job_terminal_diagnostics: clean-SUCCESS provenance ──────────────


def test_success_job_without_terminal_reason_has_run_provenance():
    """A clean-SUCCESS job with no terminal_reason must still surface
    executing_backend, runner_target_id, and runner_hostname so the
    run-provenance panel is not blank on successful runs."""
    job = MagicMock()
    job.terminal_reason = None
    job.started_at = None
    job.accelerator_type = None
    job.machine_type = None
    job.backend = "runner-subprocess"
    job.polling_metadata = {
        "runner_target_id": "runner-xyz-456",
        "runner_hostname": "gpu-host-1",
    }

    diag = _extract_job_terminal_diagnostics(job)

    assert diag is not None, "provenance must be non-None even without terminal_reason"
    assert diag["executing_backend"] == "runner-subprocess"
    assert diag["runner_target_id"] == "runner-xyz-456"
    assert diag["runner_hostname"] == "gpu-host-1"
    assert "terminal_reason" not in diag, "terminal_reason must be absent on SUCCESS run"
    assert "vm_phase" not in diag, "vm_phase must be absent when no terminal_reason"

    # _build_compute_experiment_document must surface the provenance in body
    task = _base_task()
    doc = _build_compute_experiment_document(
        task,
        outcome="SUCCESS",
        cost=0.12,
        criterion=None,
        result_reference=None,
        terminal_diagnostics=diag,
    )

    body = doc["body"]
    assert "Executing backend:" in body, "body must show executing backend"
    assert "runner-subprocess" in body
    assert "Runner:" in body, "body must show runner identity"
    assert "runner-xyz-456" in body
    assert "gpu-host-1" in body

    td = doc["verdict_record"].get("terminal_diagnostics", {})
    assert td["executing_backend"] == "runner-subprocess"
    assert td["runner_target_id"] == "runner-xyz-456"
    assert td["runner_hostname"] == "gpu-host-1"
