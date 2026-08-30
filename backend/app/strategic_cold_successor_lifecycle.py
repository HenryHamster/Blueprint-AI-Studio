"""Fresh, transcript-free strategic successor launch and attestation."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    KnowledgeDocument,
    Project,
    SourceKind,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.max_plan_pool import get_task_llm_account_id, release_max_plan_account
from app.payload_schemas import validate_internal_payload
from app.strategic_cold_successor import (
    load_locked_cold_successor_contract,
    serialize_cold_successor_contract,
)
from app.strategic_cold_successor_authority import (
    COLD_SUCCESSOR_CONTEXT_KEY,
    COLD_SUCCESSOR_LAUNCH_MODE,
    cold_successor_blob_matches_task,
    cold_successor_blob_owns_task,
)
from app.strategic_resident_session import (
    RESIDENT_INFRASTRUCTURE_CONTEXT_KEY,
    RESIDENT_SESSION_KEY,
    RESIDENT_TASK_PRIORITY,
    STATUS_ACTIVE,
    STATUS_ENDED,
    _LIVE_TASK_STATUSES,
    _RESIDENT_WORKER_INCARNATION_PROTOCOL,
    _resolve_override_runner,
    _resident_runner_is_available,
    claim_resident_worker_incarnation,
    new_session_id,
    read_resident_session,
    resident_session_is_active,
    resident_worker_incarnation_is_current,
)
from app.strategic_memory_sources import canonical_json_bytes
from app.task_locking import project_lock, task_lock, with_vision_locked
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)

COLD_SUCCESSOR_ATTESTATION_KEY = "_cold_successor_attestation"
COLD_SUCCESSOR_TERMINAL_KEY = "_cold_successor_terminal"
COLD_SUCCESSOR_COMPLETION_EFFECT_KEY = "_cold_successor_completion_terminal_effect_id"
_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ColdSuccessorStartRequest(BaseModel):
    runner_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_predecessor_session_id: str = Field(min_length=1, max_length=255)
    expected_predecessor_task_id: uuid.UUID | None = None
    expected_predecessor_worker_generation: int = Field(ge=1)
    expected_core_sha256: str

    @field_validator("idempotency_key", "expected_predecessor_session_id")
    @classmethod
    def _strip_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("expected_core_sha256")
    @classmethod
    def _lower_core_sha256(cls, value: str) -> str:
        value = value.strip().lower()
        if not _HASH_RE.fullmatch(value):
            raise ValueError("value must be lower-case SHA-256")
        return value


class ColdSuccessorHeartbeatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=255)
    task_id: uuid.UUID
    worker_generation: int = Field(ge=1)
    resident_incarnation_id: uuid.UUID


class ColdSuccessorReceiptRequest(ColdSuccessorHeartbeatRequest):
    """Exact identity required to read the durable proof receipt."""


class ColdSuccessorAttestationRequest(ColdSuccessorHeartbeatRequest):
    core_sha256: str
    problem_model_sha256: str
    running_update_sha256: str
    strategic_memory_sha256: str
    provider_terminal_reason: Literal["completed"]
    provider_output_sha256: str
    semantic_result: str = Field(min_length=1, max_length=4000)

    @field_validator(
        "core_sha256",
        "problem_model_sha256",
        "running_update_sha256",
        "strategic_memory_sha256",
        "provider_output_sha256",
    )
    @classmethod
    def _lower_sha256(cls, value: str) -> str:
        value = value.strip().lower()
        if not _HASH_RE.fullmatch(value):
            raise ValueError("value must be lower-case SHA-256")
        return value


class ColdSuccessorTerminalRequest(ColdSuccessorHeartbeatRequest):
    terminal_effect_id: uuid.UUID
    status: Literal["COMPLETED", "FAILED"]
    terminal_reason: Literal[
        "completed",
        "provider_terminal_failed",
        "provider_result_invalid",
        "authority_renewal_failed",
        "attestation_failed",
        "internal_error",
    ]
    diagnostic_sha256: str | None = None

    @field_validator("diagnostic_sha256")
    @classmethod
    def _optional_lower_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not _HASH_RE.fullmatch(value):
            raise ValueError("value must be lower-case SHA-256")
        return value

    @model_validator(mode="after")
    def _coherent_terminal_reason(self):
        if self.status == "COMPLETED":
            if self.terminal_reason != "completed" or self.diagnostic_sha256 is not None:
                raise ValueError("completed terminal effects must be exact")
        elif self.terminal_reason == "completed" or self.diagnostic_sha256 is None:
            raise ValueError(
                "failed terminal effects require a failure reason and diagnostic"
            )
        return self


def _idempotency_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _task_idempotency_key(project_id: uuid.UUID, fingerprint: str) -> str:
    return f"cold-successor:{project_id}:{fingerprint}"


def _launch_effect_sha256(
    project_id: uuid.UUID,
    request: ColdSuccessorStartRequest,
) -> str:
    effect = {
        "project_id": str(project_id),
        "runner_id": str(request.runner_id),
        "expected_predecessor_session_id": request.expected_predecessor_session_id,
        "expected_predecessor_task_id": (
            str(request.expected_predecessor_task_id)
            if request.expected_predecessor_task_id
            else None
        ),
        "expected_predecessor_worker_generation": (
            request.expected_predecessor_worker_generation
        ),
        "expected_core_sha256": request.expected_core_sha256,
    }
    return hashlib.sha256(
        json.dumps(effect, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _predecessor_identity(blob: dict[str, Any] | None) -> tuple[str, str | None, int]:
    if not isinstance(blob, dict):
        return "", None, 0
    try:
        generation = int(blob.get("worker_generation") or 1)
    except (TypeError, ValueError):
        generation = 0
    task_id = str(blob.get("task_id")) if blob.get("task_id") else None
    return str(blob.get("session_id") or ""), task_id, generation


def _blob_identity(blob: Any) -> tuple[str, str, int] | None:
    if not isinstance(blob, dict):
        return None
    try:
        generation = int(blob.get("worker_generation"))
    except (TypeError, ValueError):
        return None
    session_id = blob.get("session_id")
    task_id = blob.get("task_id")
    if not isinstance(session_id, str) or not session_id or not task_id or generation < 1:
        return None
    return session_id, str(task_id), generation


def is_cold_successor_blob(blob: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(blob, dict)
        and blob.get("launch_mode") == COLD_SUCCESSOR_LAUNCH_MODE
    )


async def preview_cold_successor(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    payload = await load_locked_cold_successor_contract(session, project_id)
    contract = payload["cold_successor_contract"]
    manifest = contract["manifest"]
    return {
        "kind": "blueprint.cold_successor_preview.v1",
        "project_id": str(project_id),
        "core_sha256": manifest["core"]["sha256"],
        "problem_model_sha256": manifest["problem_model"]["sha256"],
        "running_update_sha256": manifest["running_update"]["sha256"],
        "strategic_memory_sha256": manifest["strategic_memory"]["sha256"],
        "strategic_memory_document_count": manifest["strategic_memory"][
            "document_count"
        ],
        "payload_byte_count": len(canonical_json_bytes(payload)),
    }


def _manifest(task: Task) -> dict[str, Any]:
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    contract = payload.get("cold_successor_contract")
    manifest = contract.get("manifest") if isinstance(contract, dict) else None
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=409, detail="Cold successor manifest is missing")
    return manifest


def _attestation_hashes_match_manifest(receipt: Any, manifest: dict[str, Any]) -> bool:
    if not isinstance(receipt, dict):
        return False
    sections = {
        key: manifest.get(key) if isinstance(manifest.get(key), dict) else {}
        for key in ("core", "problem_model", "running_update", "strategic_memory")
    }
    expected = {
        "core_sha256": sections["core"].get("sha256"),
        "problem_model_sha256": sections["problem_model"].get("sha256"),
        "running_update_sha256": sections["running_update"].get("sha256"),
        "strategic_memory_sha256": sections["strategic_memory"].get("sha256"),
    }
    return all(
        isinstance(value, str)
        and _HASH_RE.fullmatch(value)
        and receipt.get(key) == value
        for key, value in expected.items()
    )


def _durable_attestation_is_exact(
    receipt: Any,
    manifest: dict[str, Any],
    *,
    blob: dict[str, Any],
    task: Task,
) -> bool:
    identity = _blob_identity(blob)
    try:
        uuid.UUID(str(receipt.get("completion_terminal_effect_id")))
        uuid.UUID(str(receipt.get("resident_incarnation_id")))
        receipt_generation = int(receipt.get("worker_generation"))
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(
        identity is not None
        and _attestation_hashes_match_manifest(receipt, manifest)
        and receipt.get("schema_version") == 1
        and receipt.get("session_id") == identity[0]
        and receipt.get("task_id") == str(task.id)
        and receipt_generation == identity[2]
        and receipt.get("provider_terminal_reason") == "completed"
        and isinstance(receipt.get("provider_output_sha256"), str)
        and _HASH_RE.fullmatch(receipt["provider_output_sha256"])
        and isinstance(receipt.get("semantic_result"), str)
        and receipt["semantic_result"].strip()
        and len(receipt["semantic_result"]) <= 4000
        and isinstance(receipt.get("attested_at"), str)
        and receipt["attested_at"]
    )


def _durable_success_terminal_is_exact(
    receipt: Any,
    *,
    blob: dict[str, Any],
    task: Task,
) -> bool:
    identity = _blob_identity(blob)
    if not isinstance(receipt, dict) or identity is None:
        return False
    attestation = (task.context or {}).get(COLD_SUCCESSOR_ATTESTATION_KEY)
    try:
        terminal_effect_id = uuid.UUID(str(receipt.get("terminal_effect_id")))
        completion_effect_id = uuid.UUID(
            str((task.context or {}).get(COLD_SUCCESSOR_COMPLETION_EFFECT_KEY))
        )
        attested_completion_effect_id = uuid.UUID(
            str(attestation.get("completion_terminal_effect_id"))
        )
        receipt_incarnation = uuid.UUID(
            str(receipt.get("resident_incarnation_id"))
        )
        blob_incarnation = uuid.UUID(str(blob.get("worker_incarnation_id")))
        receipt_generation = int(receipt.get("worker_generation"))
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(
        receipt.get("schema_version") == 1
        and receipt.get("session_id") == identity[0]
        and receipt.get("task_id") == str(task.id)
        and receipt_generation == identity[2]
        and receipt_incarnation == blob_incarnation
        and terminal_effect_id == completion_effect_id
        and attested_completion_effect_id == completion_effect_id
        and receipt.get("status") == "COMPLETED"
        and receipt.get("terminal_reason") == "completed"
        and receipt.get("diagnostic_sha256") is None
        and isinstance(receipt.get("recorded_at"), str)
        and receipt["recorded_at"]
    )


def cold_successor_projection(blob: dict[str, Any], task: Task) -> dict[str, Any]:
    identity = _blob_identity(blob)
    if identity is None:
        raise HTTPException(status_code=409, detail="Cold successor identity is malformed")
    manifest = _manifest(task)
    core_section = manifest.get("core")
    if not isinstance(core_section, dict):
        raise HTTPException(status_code=409, detail="Cold successor manifest is malformed")
    receipt = (task.context or {}).get(COLD_SUCCESSOR_ATTESTATION_KEY)
    terminal = (task.context or {}).get(COLD_SUCCESSOR_TERMINAL_KEY)
    attested = _durable_attestation_is_exact(
        receipt,
        manifest,
        blob=blob,
        task=task,
    )
    terminal_succeeded = _durable_success_terminal_is_exact(
        terminal,
        blob=blob,
        task=task,
    )
    if task.status == TaskStatus.COMPLETED and attested and terminal_succeeded:
        proof_state = "succeeded"
    elif task.status in _TERMINAL:
        proof_state = "failed"
    elif attested:
        proof_state = "finalizing"
    elif task.started_at is not None:
        proof_state = "running"
    else:
        proof_state = "pending"
    return {
        "launch_mode": COLD_SUCCESSOR_LAUNCH_MODE,
        "proof_state": proof_state,
        "session_id": identity[0],
        "task_id": str(task.id),
        "worker_generation": identity[2],
        "runner_id": str(blob.get("runner_id") or "") or None,
        "idempotency_fingerprint": blob.get("idempotency_fingerprint"),
        "launch_effect_sha256": blob.get("launch_effect_sha256"),
        "core_sha256": core_section.get("sha256"),
        "task_status": getattr(task.status, "value", task.status),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "last_heartbeat": task.last_heartbeat.isoformat() if task.last_heartbeat else None,
        "attestation": receipt if isinstance(receipt, dict) else None,
        "terminal": terminal if isinstance(terminal, dict) else None,
        "ended_at": blob.get("ended_at"),
        "failure_reason": blob.get("proof_failure_reason"),
    }


async def _load_memory(session: AsyncSession, project_id: uuid.UUID):
    return (
        await session.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.project_id == project_id,
                KnowledgeDocument.source_kind == SourceKind.STRATEGIC_MEMORY.value,
                KnowledgeDocument.lifecycle_status == "consumed",
            )
            .order_by(KnowledgeDocument.created_at.asc(), KnowledgeDocument.id.asc())
        )
    ).scalars().all()


async def launch_cold_successor(
    session: AsyncSession,
    project_id: uuid.UUID,
    request: ColdSuccessorStartRequest,
    *,
    operator_user_id: str,
) -> dict[str, Any]:
    fingerprint = _idempotency_fingerprint(request.idempotency_key)
    launch_effect_sha256 = _launch_effect_sha256(project_id, request)
    task_key = _task_idempotency_key(project_id, fingerprint)
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        blob = read_resident_session(project.vision)
        if is_cold_successor_blob(blob) and blob.get("idempotency_fingerprint") == fingerprint:
            if blob.get("launch_effect_sha256") != launch_effect_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor idempotency key belongs to another effect",
                )
            try:
                replay_task_id = uuid.UUID(str(blob.get("task_id")))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=409, detail="Cold successor receipt is inconsistent"
                ) from None
            task = await session.get(Task, replay_task_id)
            if (
                task is None
                or task.project_id != project.id
                or not cold_successor_blob_matches_task(blob, task)
            ):
                raise HTTPException(status_code=409, detail="Cold successor receipt is inconsistent")
            return cold_successor_projection(blob, task)
        expected = (
            request.expected_predecessor_session_id,
            str(request.expected_predecessor_task_id) if request.expected_predecessor_task_id else None,
            request.expected_predecessor_worker_generation,
        )
        if _predecessor_identity(blob) != expected:
            raise HTTPException(status_code=409, detail="Predecessor session identity changed")
        if resident_session_is_active(blob):
            raise HTTPException(status_code=409, detail="Predecessor Resident is still active")
        if project.paused is not True or project.autonomous_enabled is not False:
            raise HTTPException(status_code=409, detail="Cold successor requires pause and autonomy off")
        if project.archived:
            raise HTTPException(status_code=409, detail="Archived project cannot launch a successor")
        runner = await _resolve_override_runner(session, project.id, str(request.runner_id))
        if runner is None:
            raise HTTPException(status_code=404, detail="Selected device not found for this project")
        if not _resident_runner_is_available(runner):
            raise HTTPException(status_code=409, detail="Selected device is not available")
        live_tasks = (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.status.in_(list(_LIVE_TASK_STATUSES)),
                )
            )
        ).scalars().all()
        if live_tasks:
            raise HTTPException(status_code=409, detail="Project work is still active")

        payload = serialize_cold_successor_contract(
            project,
            await _load_memory(session, project.id),
        )
        core_sha256 = payload["cold_successor_contract"]["manifest"]["core"][
            "sha256"
        ]
        if core_sha256 != request.expected_core_sha256:
            raise HTTPException(
                status_code=409,
                detail="Cold successor durable source core changed",
            )
        payload = validate_internal_payload(
            payload,
            "strategic_cold_successor_lifecycle:launch_cold_successor",
        )
        session_id = new_session_id()
        generation = 1
        context = {
            "_resident_session": True,
            RESIDENT_INFRASTRUCTURE_CONTEXT_KEY: True,
            COLD_SUCCESSOR_CONTEXT_KEY: True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": generation,
            "runner_target_id": str(runner.id),
            "preferred_runner_target_id": str(runner.id),
            "_cold_successor_core_sha256": core_sha256,
            "_cold_successor_idempotency_fingerprint": fingerprint,
            "_cold_successor_launch_effect_sha256": launch_effect_sha256,
            COLD_SUCCESSOR_COMPLETION_EFFECT_KEY: str(uuid.uuid4()),
        }
        task = Task(
            project_id=project.id,
            title="Cold strategic successor proof",
            description="Transcript-free durable strategic authority handoff",
            payloads=[payload],
            context=context,
            created_by="ORCHESTRATOR",
            launched_by_user_id=operator_user_id,
            idempotency_key=task_key,
            priority=RESIDENT_TASK_PRIORITY,
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            vm_provisioned_at=_utcnow(),
        )
        session.add(task)
        await session.flush()
        session.add(
            TaskHistory(
                task_id=task.id,
                action="SPAWNED",
                details={"spawned_by": "COLD_SUCCESSOR", "core_sha256": core_sha256},
            )
        )
        now_iso = _utcnow().isoformat()
        vision = dict(project.vision or {})
        vision[RESIDENT_SESSION_KEY] = {
            "version": 2,
            "launch_mode": COLD_SUCCESSOR_LAUNCH_MODE,
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": generation,
            "worker_incarnation_protocol": _RESIDENT_WORKER_INCARNATION_PROTOCOL,
            "worker_state": "starting",
            "runner_id": str(runner.id),
            "operator_user_id": operator_user_id,
            "status": STATUS_ACTIVE,
            "stop_requested": False,
            "started_at": now_iso,
            "updated_at": now_iso,
            "ended_at": None,
            "idempotency_fingerprint": fingerprint,
            "launch_effect_sha256": launch_effect_sha256,
            "core_sha256": core_sha256,
            "proof_state": "pending",
        }
        await with_vision_locked(
            session,
            project.id,
            lambda current_vision: current_vision.__setitem__(
                RESIDENT_SESSION_KEY,
                vision[RESIDENT_SESSION_KEY],
            ),
            _locked_project=project,
        )
        await session.commit()
        await session.refresh(task)
        return cold_successor_projection(vision[RESIDENT_SESSION_KEY], task)


async def heartbeat_cold_successor(
    session: AsyncSession,
    project_id: uuid.UUID,
    request: ColdSuccessorHeartbeatRequest,
) -> dict[str, Any]:
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        vision = dict(project.vision or {})
        blob = read_resident_session(vision)
        if not is_cold_successor_blob(blob) or _blob_identity(blob) != (
            request.session_id,
            str(request.task_id),
            request.worker_generation,
        ):
            raise HTTPException(status_code=409, detail="Cold successor identity mismatch")
        async with task_lock(session, request.task_id, skip_locked=False) as task:
            if (
                task is None
                or task.project_id != project.id
                or not cold_successor_blob_matches_task(blob, task)
            ):
                raise HTTPException(status_code=409, detail="Cold successor task mismatch")
            if not cold_successor_blob_owns_task(blob, task) or task.status != TaskStatus.IN_PROGRESS:
                raise HTTPException(status_code=409, detail="Cold successor is not active")
            held = claim_resident_worker_incarnation(
                vision,
                task_id=task.id,
                worker_generation=request.worker_generation,
                incarnation_id=request.resident_incarnation_id,
            )
            if held != request.resident_incarnation_id:
                return {"authority_held": False, "resident_incarnation_held": False}
            now = _utcnow()
            current = dict(read_resident_session(vision) or {})
            current.update(
                worker_state="running",
                worker_heartbeat_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            vision[RESIDENT_SESSION_KEY] = current
            await with_vision_locked(
                session,
                project.id,
                lambda current_vision: current_vision.__setitem__(
                    RESIDENT_SESSION_KEY,
                    current,
                ),
                _locked_project=project,
            )
            task.last_heartbeat = now
            session.add(task)
            await session.commit()
            return {
                "authority_held": True,
                "resident_incarnation_held": True,
                "stop_requested": bool(current.get("stop_requested")),
                "status": current.get("status"),
                "leased_until": current.get("worker_incarnation_expires_at"),
            }


async def read_cold_successor_receipt(
    session: AsyncSession,
    project_id: uuid.UUID,
    request: ColdSuccessorReceiptRequest,
) -> dict[str, Any]:
    """Read one exact durable receipt without mutating or reconciling state."""

    row = (
        await session.execute(
            select(Project, Task)
            .join(Task, Task.project_id == Project.id)
            .where(Project.id == project_id, Task.id == request.task_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Cold successor proof not found")
    project, task = row
    blob = read_resident_session(project.vision)
    if not is_cold_successor_blob(blob) or _blob_identity(blob) != (
        request.session_id,
        str(request.task_id),
        request.worker_generation,
    ):
        raise HTTPException(status_code=409, detail="Cold successor identity mismatch")
    if not cold_successor_blob_matches_task(blob, task):
        raise HTTPException(status_code=409, detail="Cold successor task mismatch")
    completion_effect_id = (task.context or {}).get(
        COLD_SUCCESSOR_COMPLETION_EFFECT_KEY
    )
    try:
        completion_effect_id = str(uuid.UUID(str(completion_effect_id)))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=409,
            detail="Cold successor completion authority is malformed",
        ) from None
    attestation = (task.context or {}).get(COLD_SUCCESSOR_ATTESTATION_KEY)
    terminal = (task.context or {}).get(COLD_SUCCESSOR_TERMINAL_KEY)
    if attestation is not None and not isinstance(attestation, dict):
        raise HTTPException(
            status_code=409,
            detail="Cold successor attestation receipt is malformed",
        )
    if terminal is not None and not isinstance(terminal, dict):
        raise HTTPException(
            status_code=409,
            detail="Cold successor terminal receipt is malformed",
        )
    if isinstance(terminal, dict):
        if attestation is not None and (
            not _durable_attestation_is_exact(
                attestation,
                _manifest(task),
                blob=blob,
                task=task,
            )
            or attestation.get("completion_terminal_effect_id")
            != completion_effect_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Cold successor attestation receipt is inconsistent",
            )
        if terminal.get("status") not in {"COMPLETED", "FAILED"}:
            raise HTTPException(
                status_code=409,
                detail="Cold successor terminal receipt is inconsistent",
            )
        expected_status = (
            TaskStatus.COMPLETED
            if terminal.get("status") == "COMPLETED"
            else TaskStatus.FAILED
        )
        try:
            uuid.UUID(str(terminal.get("terminal_effect_id")))
            receipt_incarnation = uuid.UUID(
                str(terminal.get("resident_incarnation_id"))
            )
            blob_incarnation = uuid.UUID(str(blob.get("worker_incarnation_id")))
            terminal_generation = int(terminal.get("worker_generation"))
        except (TypeError, ValueError, AttributeError):
            common_exact = False
        else:
            common_exact = bool(
                terminal.get("schema_version") == 1
                and terminal.get("session_id") == request.session_id
                and terminal.get("task_id") == str(request.task_id)
                and terminal_generation == request.worker_generation
                and receipt_incarnation == blob_incarnation
                and isinstance(terminal.get("recorded_at"), str)
                and terminal["recorded_at"]
                and task.status == expected_status
                and blob.get("status") == STATUS_ENDED
                and blob.get("proof_state")
                == (
                    "succeeded"
                    if expected_status == TaskStatus.COMPLETED
                    else "failed"
                )
            )
        if expected_status == TaskStatus.COMPLETED:
            terminal_exact = _durable_success_terminal_is_exact(
                terminal,
                blob=blob,
                task=task,
            )
        else:
            terminal_exact = bool(
                common_exact
                and attestation is None
                and terminal.get("terminal_reason")
                in {
                    "provider_terminal_failed",
                    "provider_result_invalid",
                    "authority_renewal_failed",
                    "attestation_failed",
                    "internal_error",
                }
                and isinstance(terminal.get("diagnostic_sha256"), str)
                and _HASH_RE.fullmatch(terminal["diagnostic_sha256"])
            )
        if not common_exact or not terminal_exact:
            raise HTTPException(
                status_code=409,
                detail="Cold successor terminal receipt is inconsistent",
            )
        return {
            "schema_version": 1,
            "receipt_state": "terminal",
            "session_id": request.session_id,
            "task_id": str(request.task_id),
            "worker_generation": request.worker_generation,
            "task_status": getattr(task.status, "value", task.status),
            "completion_terminal_effect_id": completion_effect_id,
            "attestation": attestation if isinstance(attestation, dict) else None,
            "terminal": terminal,
        }
    if task.status in _TERMINAL or blob.get("status") != STATUS_ACTIVE:
        raise HTTPException(
            status_code=409,
            detail="Cold successor terminal receipt is missing",
        )
    if not cold_successor_blob_owns_task(blob, task) or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Cold successor is not active")
    if not resident_worker_incarnation_is_current(
        project.vision,
        task_id=request.task_id,
        worker_generation=request.worker_generation,
        incarnation_id=request.resident_incarnation_id,
    ):
        raise HTTPException(status_code=409, detail="Cold successor incarnation is stale")
    if attestation is not None and (
        not _durable_attestation_is_exact(
            attestation,
            _manifest(task),
            blob=blob,
            task=task,
        )
        or attestation.get("completion_terminal_effect_id") != completion_effect_id
    ):
        raise HTTPException(
            status_code=409,
            detail="Cold successor attestation receipt is inconsistent",
        )
    return {
        "schema_version": 1,
        "receipt_state": "attested" if isinstance(attestation, dict) else "pending",
        "session_id": request.session_id,
        "task_id": str(request.task_id),
        "worker_generation": request.worker_generation,
        "task_status": getattr(task.status, "value", task.status),
        "completion_terminal_effect_id": completion_effect_id,
        "attestation": attestation if isinstance(attestation, dict) else None,
        "terminal": None,
    }


async def attest_cold_successor(
    session: AsyncSession,
    project_id: uuid.UUID,
    request: ColdSuccessorAttestationRequest,
) -> dict[str, Any]:
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        blob = read_resident_session(project.vision)
        if not is_cold_successor_blob(blob) or _blob_identity(blob) != (
            request.session_id,
            str(request.task_id),
            request.worker_generation,
        ):
            raise HTTPException(status_code=409, detail="Cold successor identity mismatch")
        async with task_lock(session, request.task_id, skip_locked=False) as task:
            if (
                task is None
                or task.project_id != project.id
                or not cold_successor_blob_matches_task(blob, task)
            ):
                raise HTTPException(status_code=409, detail="Cold successor task mismatch")
            manifest = _manifest(task)
            effect = {
                "schema_version": 1,
                "session_id": request.session_id,
                "task_id": str(request.task_id),
                "worker_generation": request.worker_generation,
                "resident_incarnation_id": str(request.resident_incarnation_id),
                "core_sha256": request.core_sha256,
                "problem_model_sha256": request.problem_model_sha256,
                "running_update_sha256": request.running_update_sha256,
                "strategic_memory_sha256": request.strategic_memory_sha256,
                "provider_terminal_reason": request.provider_terminal_reason,
                "provider_output_sha256": request.provider_output_sha256,
                "semantic_result": request.semantic_result,
            }
            if not _attestation_hashes_match_manifest(effect, manifest):
                raise HTTPException(status_code=409, detail="Cold successor hashes do not match")
            existing = (task.context or {}).get(COLD_SUCCESSOR_ATTESTATION_KEY)
            if isinstance(existing, dict):
                comparable = {key: existing.get(key) for key in effect}
                if comparable != effect:
                    raise HTTPException(status_code=409, detail="Cold successor attestation differs")
                return {"status": "duplicate", "attestation": existing}
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor attestation receipt is malformed",
                )
            if not cold_successor_blob_owns_task(blob, task) or task.status != TaskStatus.IN_PROGRESS:
                raise HTTPException(status_code=409, detail="Cold successor is not active")
            if not resident_worker_incarnation_is_current(
                project.vision,
                task_id=request.task_id,
                worker_generation=request.worker_generation,
                incarnation_id=request.resident_incarnation_id,
            ):
                raise HTTPException(status_code=409, detail="Cold successor incarnation is stale")
            completion_effect_id = (task.context or {}).get(
                COLD_SUCCESSOR_COMPLETION_EFFECT_KEY
            )
            try:
                completion_effect_id = str(uuid.UUID(str(completion_effect_id)))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor completion authority is malformed",
                ) from None
            receipt = {
                **effect,
                "completion_terminal_effect_id": completion_effect_id,
                "attested_at": _utcnow().isoformat(),
            }
            context = dict(task.context or {})
            context[COLD_SUCCESSOR_ATTESTATION_KEY] = receipt
            task.context = context
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return {"status": "attested", "attestation": receipt}


async def terminal_cold_successor(
    session: AsyncSession,
    project_id: uuid.UUID,
    request: ColdSuccessorTerminalRequest,
) -> dict[str, Any]:
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        vision = dict(project.vision or {})
        blob = read_resident_session(vision)
        if not is_cold_successor_blob(blob) or _blob_identity(blob) != (
            request.session_id,
            str(request.task_id),
            request.worker_generation,
        ):
            raise HTTPException(status_code=409, detail="Cold successor identity mismatch")
        async with task_lock(session, request.task_id, skip_locked=False) as task:
            if (
                task is None
                or task.project_id != project.id
                or not cold_successor_blob_matches_task(blob, task)
            ):
                raise HTTPException(status_code=409, detail="Cold successor task mismatch")
            effect = {
                "schema_version": 1,
                "terminal_effect_id": str(request.terminal_effect_id),
                "session_id": request.session_id,
                "task_id": str(request.task_id),
                "worker_generation": request.worker_generation,
                "resident_incarnation_id": str(request.resident_incarnation_id),
                "status": request.status,
                "terminal_reason": request.terminal_reason,
                "diagnostic_sha256": request.diagnostic_sha256,
            }
            task_context = task.context or {}
            receipt = task_context.get(COLD_SUCCESSOR_ATTESTATION_KEY)
            if receipt is not None and not isinstance(receipt, dict):
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor attestation receipt is malformed",
                )
            existing = task_context.get(COLD_SUCCESSOR_TERMINAL_KEY)
            if isinstance(existing, dict):
                if request.status == "FAILED" and receipt is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Cold successor attestation cannot be downgraded",
                    )
                if {key: existing.get(key) for key in effect} != effect:
                    raise HTTPException(
                        status_code=409, detail="Cold successor terminal effect differs"
                    )
                expected_status = (
                    TaskStatus.COMPLETED
                    if request.status == "COMPLETED"
                    else TaskStatus.FAILED
                )
                if (
                    task.status != expected_status
                    or blob.get("status") != STATUS_ENDED
                    or blob.get("proof_state")
                    != ("succeeded" if request.status == "COMPLETED" else "failed")
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Cold successor terminal receipt is inconsistent",
                    )
                return {
                    "status": "duplicate",
                    "proof": cold_successor_projection(blob, task),
                }
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor terminal receipt is malformed",
                )
            if task.status in _TERMINAL:
                raise HTTPException(
                    status_code=409, detail="Cold successor task is already terminal"
                )
            if not cold_successor_blob_owns_task(blob, task) or task.status != TaskStatus.IN_PROGRESS:
                raise HTTPException(status_code=409, detail="Cold successor is not active")
            if not resident_worker_incarnation_is_current(
                vision,
                task_id=request.task_id,
                worker_generation=request.worker_generation,
                incarnation_id=request.resident_incarnation_id,
            ):
                raise HTTPException(status_code=409, detail="Cold successor incarnation is stale")
            success = request.status == "COMPLETED"
            if not success and receipt is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor attestation cannot be downgraded",
                )
            if success and not _durable_attestation_is_exact(
                receipt,
                _manifest(task),
                blob=blob,
                task=task,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor completion requires exact attestation",
                )
            completion_effect_id = (task.context or {}).get(
                COLD_SUCCESSOR_COMPLETION_EFFECT_KEY
            )
            if success and (
                str(request.terminal_effect_id) != completion_effect_id
                or receipt.get("completion_terminal_effect_id")
                != completion_effect_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Cold successor completion effect is not server-authorized",
                )
            now = _utcnow()
            now_iso = now.isoformat()
            context = dict(task.context or {})
            context[COLD_SUCCESSOR_TERMINAL_KEY] = {
                **effect,
                "recorded_at": now_iso,
            }
            account_id = get_task_llm_account_id(task)
            release_slot_after_commit = bool(
                account_id and not context.get("_slot_released")
            )
            if release_slot_after_commit:
                context["_slot_released"] = True
            task.context = context
            flag_modified(task, "context")
            task.status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
            task.vm_stopped_at = now
            task.latest_status_message = (
                "Cold successor proof attested"
                if success
                else f"Cold successor proof failed: {request.terminal_reason}"
            )
            ended = dict(blob)
            ended.update(
                status=STATUS_ENDED,
                stop_requested=True,
                worker_state="completed" if success else "failed",
                proof_state="succeeded" if success else "failed",
                proof_failure_reason=(None if success else request.terminal_reason),
                ended_at=now_iso,
                updated_at=now_iso,
            )
            vision[RESIDENT_SESSION_KEY] = ended
            await with_vision_locked(
                session,
                project.id,
                lambda current_vision: current_vision.__setitem__(
                    RESIDENT_SESSION_KEY,
                    ended,
                ),
                _locked_project=project,
            )
            session.add(task)
            session.add(
                TaskHistory(
                    task_id=task.id,
                    action="COLD_SUCCESSOR_TERMINAL",
                    details={
                        "terminal_effect_id": str(request.terminal_effect_id),
                        "status": request.status,
                        "terminal_reason": request.terminal_reason,
                        "attestation_sha256": (
                            hashlib.sha256(
                                json.dumps(
                                    receipt,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest()
                            if success
                            else None
                        ),
                    },
                )
            )
            await session.commit()
            if release_slot_after_commit and account_id:
                try:
                    await release_max_plan_account(
                        account_id,
                        project_id=str(project.id),
                    )
                except Exception:
                    logger.exception(
                        "Cold successor %s committed before account-slot release; "
                        "pool reconciliation will heal the conservative overcount",
                        task.id,
                    )
            return {
                "status": "accepted",
                "proof": cold_successor_projection(ended, task),
            }


async def reconcile_cold_successor_terminal(
    session: AsyncSession,
    task_id: uuid.UUID | str,
) -> bool:
    try:
        task_uuid = uuid.UUID(str(task_id))
    except (TypeError, ValueError):
        return False
    project_id = (
        await session.execute(select(Task.project_id).where(Task.id == task_uuid))
    ).scalar_one_or_none()
    if project_id is None:
        return False
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is None:
            return False
        async with task_lock(session, task_uuid, skip_locked=False) as task:
            if (
                task is None
                or task.status not in _TERMINAL
                or task.project_id != project.id
            ):
                return False
            vision = dict(project.vision or {})
            blob = read_resident_session(vision)
            if not cold_successor_blob_matches_task(blob, task):
                return False
            if blob.get("status") == STATUS_ENDED:
                return True
            context = task.context or {}
            receipt = context.get(COLD_SUCCESSOR_ATTESTATION_KEY)
            terminal = context.get(COLD_SUCCESSOR_TERMINAL_KEY)
            success = bool(
                task.status == TaskStatus.COMPLETED
                and _durable_attestation_is_exact(
                    receipt,
                    _manifest(task),
                    blob=blob,
                    task=task,
                )
                and _durable_success_terminal_is_exact(
                    terminal,
                    blob=blob,
                    task=task,
                )
            )
            failure_reason = None
            if not success:
                failure_reason = (
                    "terminal_without_exact_attestation"
                    if not _durable_attestation_is_exact(
                        receipt,
                        _manifest(task),
                        blob=blob,
                        task=task,
                    )
                    else "terminal_without_exact_dedicated_receipt"
                )
            now_iso = _utcnow().isoformat()
            ended = dict(blob)
            ended.update(
                status=STATUS_ENDED,
                stop_requested=True,
                worker_state="completed" if success else "failed",
                proof_state="succeeded" if success else "failed",
                proof_failure_reason=failure_reason,
                ended_at=now_iso,
                updated_at=now_iso,
            )
            vision[RESIDENT_SESSION_KEY] = ended
            await with_vision_locked(
                session,
                project.id,
                lambda current_vision: current_vision.__setitem__(
                    RESIDENT_SESSION_KEY,
                    ended,
                ),
                _locked_project=project,
            )
            await session.commit()
            return True


async def cold_successor_status(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    project = await session.get(Project, project_id)
    blob = read_resident_session(project.vision if project else None)
    if project is None or not is_cold_successor_blob(blob):
        raise HTTPException(status_code=404, detail="No cold successor proof")
    identity = _blob_identity(blob)
    if identity is None:
        raise HTTPException(status_code=409, detail="Cold successor identity is malformed")
    try:
        task_id = uuid.UUID(identity[1])
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="Cold successor identity is malformed") from None
    task = await session.get(Task, task_id)
    if (
        task is None
        or task.project_id != project.id
        or not cold_successor_blob_matches_task(blob, task)
    ):
        raise HTTPException(status_code=409, detail="Cold successor task is inconsistent")
    if task.status in _TERMINAL and blob.get("status") != STATUS_ENDED:
        task_id = task.id
        await reconcile_cold_successor_terminal(session, task_id)
        session.expire_all()
        project = await session.get(Project, project_id)
        task = await session.get(Task, task_id)
        if project is None or task is None:
            raise HTTPException(status_code=409, detail="Cold successor proof disappeared")
        blob = read_resident_session(project.vision)
    return cold_successor_projection(blob or {}, task)
