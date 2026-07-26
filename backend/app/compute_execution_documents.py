"""Durable, job-owned compute execution observations.

Unlike the task-completion knowledge lane, this service records the execution
itself.  It therefore remains useful when a parent task is cancelled after the
job succeeds, or when a workload exits before the task handler can produce a
measurement verdict.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.compute_experiment_contract import decode_generated_command
from app.models import ComputeJob, KnowledgeDocument, SourceKind, Task
from app.schemas import redact_command_log_text, sanitize_rpc_diagnostic

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
_LOG_LIMIT = 16_000
_LOG_HEAD = 4_000


def _bounded_log(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("command_log") or metadata.get("runner_command_log")
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        return None
    text = redact_command_log_text(raw["text"])
    truncated = raw.get("truncated") is True
    if len(text) > _LOG_LIMIT:
        tail = _LOG_LIMIT - _LOG_HEAD
        text = f"{text[:_LOG_HEAD]}\n...[log middle elided]...\n{text[-tail:]}"
        truncated = True
    return {"text": text, "truncated": truncated, "source": raw.get("source") or "backend"}


def _safe_manifest(raw: Any) -> dict[str, Any]:
    """Preserve provenance while scrubbing legacy secret-bearing manifests."""
    if not isinstance(raw, dict):
        return {}
    manifest = dict(raw)
    contract = manifest.get("normalized_contract")
    if isinstance(contract, dict):
        contract = dict(contract)
        contract.pop("env", None)
        contract.pop("container_registry_auth", None)
        manifest["normalized_contract"] = contract
    # Round-tripping through JSON ensures nested values are presentation-safe;
    # the command-log redactor removes credential-shaped strings in the same
    # way as the operator API.
    return json.loads(redact_command_log_text(json.dumps(manifest, default=str)))


def _job_status_value(job: ComputeJob) -> str:
    return str(getattr(job.status, "value", job.status) or "unknown")


def _execution_document_fields(job: ComputeJob, task: Task | None) -> tuple[str, str, str, dict[str, Any], list[str]]:
    metadata = dict(job.polling_metadata or {})
    manifest = _safe_manifest(job.execution_manifest)
    provenance = _safe_manifest(job.runtime_provenance)
    payload = task.first_payload if task is not None else {}
    authored = (
        manifest.get("authored_command")
        or manifest.get("authored_source")
        or payload.get("command")
        or metadata.get("command")
        or ""
    )
    effective = manifest.get("effective_launcher") or metadata.get("command") or authored
    readable_authored = redact_command_log_text(decode_generated_command(authored) or str(authored))
    readable_effective = redact_command_log_text(decode_generated_command(effective) or str(effective))
    terminal_log = _bounded_log(metadata)
    metrics = _safe_manifest(
        metadata.get("result_metrics") or metadata.get("runner_result_metrics")
    )
    status = _job_status_value(job)
    outcome = getattr(job.outcome, "value", job.outcome)
    exit_code = metadata.get("exit_code")
    if exit_code is None:
        exit_code = metadata.get("runner_exit_code")
    requested = {
        "backend": job.backend,
        "machine_type": job.machine_type,
        "accelerator_type": job.accelerator_type,
        "accelerator_count": job.accelerator_count,
        "spend_cap_usd": job.spend_cap_usd,
        "deadline_seconds": metadata.get("requested_deadline_seconds"),
    }
    actual = {
        "backend": provenance.get("backend") or job.backend,
        "runner_id": provenance.get("runner_id") or (str(job.ran_on_runner_id) if job.ran_on_runner_id else None),
        "runner_hostname": provenance.get("runner_hostname") or metadata.get("runner_hostname"),
        "hardware": provenance.get("hardware") or provenance.get("observed_hardware"),
    }
    report: dict[str, Any] = {
        "job_id": str(job.id),
        "task_id": str(job.task_id) if job.task_id else None,
        "task_status": getattr(task.status, "value", task.status) if task is not None else None,
        "job_status": status,
        "job_outcome": outcome,
        "terminal_reason": job.terminal_reason,
        "exit_code": exit_code,
        "requested_execution": requested,
        "actual_execution": actual,
        "authored_source": readable_authored,
        "effective_launcher": readable_effective,
        "execution_manifest": manifest or None,
        "runtime_provenance": provenance or None,
        "metrics": metrics or None,
        "result_reference": job.result_reference,
        "artifact_id": str(job.artifact_id) if job.artifact_id else None,
        "terminal_log": terminal_log,
        "failure_classification": sanitize_rpc_diagnostic(metadata.get("compute_rpc_diagnostic")),
        "interpretation": (
            "This records execution facts only; parent task workflow state is reported separately."
        ),
    }
    summary = f"Compute job {status.lower()} ({outcome or 'no outcome'})."
    if job.terminal_reason:
        summary += f" {job.terminal_reason[:240]}"
    body = "# Compute execution observation\n\n" + json.dumps(report, indent=2, default=str)
    citations = [value for value in [job.result_reference] if isinstance(value, str) and value]
    return (
        f"Compute execution: {(task.title if task is not None else str(job.id))[:180]}",
        summary,
        body,
        report,
        citations,
    )


async def ensure_compute_execution_document(session, job_id: uuid.UUID) -> KnowledgeDocument | None:
    """Idempotently record one terminal execution document for ``job_id``.

    This intentionally does not alter task state or task-completion evidence.
    The unique ``source_compute_job_id`` is the concurrency-safe identity; a
    second terminal path simply returns the first document.
    """
    job = await session.get(ComputeJob, job_id)
    if job is None or _job_status_value(job) not in _TERMINAL_STATUSES:
        return None
    existing = (
        await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.source_compute_job_id == job.id)
        )
    ).scalars().first()
    if existing is not None:
        return existing
    task = await session.get(Task, job.task_id) if job.task_id else None
    title, summary, body, report, citations = _execution_document_fields(job, task)
    # Adopt the historical task-owned compute document when present.  This is a
    # one-time identity repair for rows written before source_compute_job_id
    # existed; it avoids showing two documents for one execution while keeping
    # any richer completed-task analysis already stored on that document.
    legacy = None
    if job.task_id is not None:
        legacy = (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.project_id == job.project_id,
                    KnowledgeDocument.source_task_id == job.task_id,
                    KnowledgeDocument.source_kind == SourceKind.COMPUTE_EXPERIMENT.value,
                    KnowledgeDocument.source_compute_job_id.is_(None),
                )
            )
        ).scalars().first()
    if legacy is not None:
        legacy.source_compute_job_id = job.id
        verdict = dict(legacy.forward_quality_verdict or {})
        verdict["execution"] = report
        legacy.forward_quality_verdict = verdict
        legacy.tags = list(dict.fromkeys([*(legacy.tags or []), "compute_execution"]))
        session.add(legacy)
        try:
            await session.commit()
            await session.refresh(legacy)
            return legacy
        except IntegrityError:
            await session.rollback()
            return (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.source_compute_job_id == job.id
                    )
                )
            ).scalars().first()
    direction_id = str((task.context or {}).get("direction_id")) if task and (task.context or {}).get("direction_id") else None
    doc = KnowledgeDocument(
        project_id=job.project_id,
        source_task_id=job.task_id,
        source_compute_job_id=job.id,
        direction_id=direction_id,
        source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
        title=title,
        summary=summary,
        body=body,
        topic="compute execution",
        tags=["compute_execution", _job_status_value(job).lower()],
        source_citations=citations,
        forward_quality_verdict={"analysis_mode": "execution_observation", "execution": report},
    )
    session.add(doc)
    try:
        await session.commit()
        await session.refresh(doc)
        return doc
    except IntegrityError:
        await session.rollback()
        return (
            await session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.source_compute_job_id == job.id)
            )
        ).scalars().first()
