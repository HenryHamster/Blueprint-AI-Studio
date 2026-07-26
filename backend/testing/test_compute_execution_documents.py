"""Focused contract tests for durable job-owned execution observations."""

import uuid

from app.compute_execution_documents import (
    _execution_document_fields,
    _refresh_execution_document,
)
from app.models import ComputeJob, ComputeJobOutcome, ComputeJobStatus, KnowledgeDocument
from app.routers.runner import _observed_runner_hardware
from app.models import LocalRunner
from app.schemas import serialize_compute_job_response_safe


def _terminal_job() -> ComputeJob:
    return ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="runner_subprocess",
        machine_type="g2-standard-4",
        accelerator_type="L4",
        accelerator_count=1,
        status=ComputeJobStatus.FAILED,
        outcome=ComputeJobOutcome.FAILURE,
        terminal_reason="process exited 17",
        execution_manifest={
            "authored_command": "python benchmark.py",
            "effective_launcher": "python benchmark.py",
            "capability_requirements": {"machine_type": "g2-standard-4"},
            "normalized_contract": {
                "command": "python benchmark.py",
                "env": {"API_KEY": "do-not-leak"},
                "container_registry_auth": {"password": "do-not-leak"},
            },
        },
        runtime_provenance={"backend": "runner_subprocess", "runner_hostname": "gpu-box"},
        polling_metadata={
            "exit_code": 17,
            "command_log": {"text": "token=do-not-leak\nactual error", "truncated": False},
            "result_metrics": {"accuracy": 0.2},
        },
    )


def test_execution_observation_keeps_job_truth_and_redacts_bounded_log():
    job = _terminal_job()

    title, summary, body, report, _citations = _execution_document_fields(job, None)

    assert title.startswith("Compute execution:")
    assert "failed" in summary
    assert report["job_status"] == "FAILED"
    assert report["exit_code"] == 17
    assert report["requested_execution"]["machine_type"] == "g2-standard-4"
    assert report["actual_execution"]["runner_hostname"] == "gpu-box"
    assert report["authored_source"] == "python benchmark.py"
    assert report["metrics"] == {"accuracy": 0.2}
    assert "do-not-leak" not in report["terminal_log"]["text"]
    assert "do-not-leak" not in body


def test_safe_compute_response_exposes_typed_terminal_log_not_manifest_secrets():
    data = serialize_compute_job_response_safe(_terminal_job())

    assert data["terminal_log"]["source"] == "backend"
    assert "do-not-leak" not in data["terminal_log"]["text"]
    assert data["execution_manifest"]["authored_command"] == "python benchmark.py"
    contract = data["execution_manifest"]["normalized_contract"]
    assert "env" not in contract
    assert "container_registry_auth" not in contract
    assert data["runtime_provenance"]["runner_hostname"] == "gpu-box"


def test_observed_runner_hardware_uses_heartbeat_inventory():
    runner = LocalRunner(
        project_id=uuid.uuid4(),
        token_hash="0" * 64,
        hostname="gpu-box",
        capabilities={
            "has_gpu": True,
            "gpu_count": 2,
            "gpu_names": ["NVIDIA L4", "NVIDIA L4"],
            "gpu_memory_total_mib": [23034, 23034],
            "active_task_ids": ["not-hardware"],
        },
    )

    hardware = _observed_runner_hardware(runner)

    assert hardware == {
        "has_gpu": True,
        "gpu_count": 2,
        "gpu_names": ["NVIDIA L4", "NVIDIA L4"],
        "gpu_memory_total_mib": [23034, 23034],
    }


def test_refresh_execution_document_replaces_stale_service_owned_log():
    job = _terminal_job()
    job.polling_metadata["command_log"] = {
        "text": "late final output",
        "truncated": False,
        "source": "runner",
    }
    title, summary, body, report, citations = _execution_document_fields(job, None)
    document = KnowledgeDocument(
        project_id=job.project_id,
        source_compute_job_id=job.id,
        title="stale",
        summary="stale",
        body="stale",
        tags=["compute_execution"],
        source_citations=[],
        forward_quality_verdict={
            "analysis_mode": "execution_observation",
            "execution": {"terminal_log": None},
        },
    )

    _refresh_execution_document(
        document,
        title=title,
        summary=summary,
        body=body,
        report=report,
        citations=citations,
    )

    assert "late final output" in document.body
    assert document.forward_quality_verdict["execution"]["terminal_log"]["source"] == "runner"
