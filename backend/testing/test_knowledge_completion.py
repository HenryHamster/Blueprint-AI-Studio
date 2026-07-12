"""Tests for knowledge_completion module — evidence_tier computation and verdict_record fields."""

from unittest.mock import MagicMock

from app.knowledge_completion import (
    _build_compute_experiment_document,
    _compute_evidence_tier,
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

    def test_local_backend_returns_covered(self):
        job = self._make_job(backend="local-bwrap")
        assert _compute_evidence_tier(job, None) == "covered"

    def test_local_container_backend_returns_covered(self):
        job = self._make_job(backend="local-container")
        assert _compute_evidence_tier(job, {"loss": 0.1}) == "covered"

    def test_runner_subprocess_backend_returns_covered(self):
        job = self._make_job(backend="runner-subprocess")
        assert _compute_evidence_tier(job, None) == "covered"

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
