"""Durable Resident qualification authority and restart-safe delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, ReleaseQualificationIntent, ReleaseVersion, Task
from app.time_utils import _utcnow


logger = logging.getLogger("blueprint")
_MODULE_SIZE_GOVERNANCE_CAP: int = 1100
QUALIFICATION_INTENT_LEASE_MINUTES = 10
QUALIFICATION_INTENT_HEARTBEAT_SECONDS = 30
QUALIFICATION_INTENT_EXECUTION_TIMEOUT_SECONDS = 8 * 60
QUALIFICATION_INTENT_RETRY_MINUTES = 5
QUALIFICATION_INTENT_MAX_ATTEMPTS = 8

_SUPERSEDED_RESULTS = {
    "no_open_version",
    "superseded",
    "superseded_phase_epoch",
    "version_not_found",
    "version_not_open",
    "wrong_phase",
}
_FAILED_RESULTS = {
    "error",
    "explicit_request_required",
    "invalid_request",
    "invalid_requested_probes",
    "no_credentials",
    "project_not_found",
    "sha_error",
    "spawn_failed",
    "unknown",
}
_RETRYABLE_RESULTS = {
    "migration_identity_deferred",
    "project_paused",
    "task_admission_deferred",
}
_APPLIED_RESULTS = {
    "already_pending",
    "cached_verdict",
    "ci_compliance_blocked",
    "deferred_unsynced",
    "external_blocker",
    "migration_identity_collision",
    "promotion_check_wait",
    "promotion_failed_requalify_cap",
    "promotion_failed_reuse_deferred",
    "rehearsal_dispatch_pending",
    "spawned",
}

# Reaper delivery is detached from the cycle watchdog. Database claims and
# leases remain the cross-process authority; this set only retains in-process
# references and bounds one service process to one slow delivery.
_background_drains: set[asyncio.Task] = set()


def _specification_hash(
    *,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    source_task_id: uuid.UUID,
    reason: str | None,
    probes: list[str],
    expected_phase_changed_at,
) -> str:
    encoded = json.dumps(
        {
            "schema_version": 1,
            "project_id": str(project_id),
            "release_version_id": str(release_version_id),
            "source_task_id": str(source_task_id),
            "reason": reason,
            "probes": sorted(probes),
            "expected_phase_changed_at": (
                expected_phase_changed_at.isoformat()
                if expected_phase_changed_at is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_authority_key(raw: str) -> str:
    normalized = raw.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("qualification authority key must be 1..200 characters")
    return normalized


def _authority_idempotency_key(
    *,
    project_id: uuid.UUID,
    authority_kind: str,
    authority_key: str,
) -> str:
    digest = hashlib.sha256(
        f"{project_id}:{authority_kind}:{authority_key}".encode("utf-8")
    ).hexdigest()
    return f"strategic-qualification:{digest}"


async def prepare_qualification_intent(
    session,
    *,
    request: object,
    task: Task,
    project: Project,
    authority_key: str | None = None,
    authority_kind: str = "resident_explicit",
) -> ReleaseQualificationIntent | None:
    """Persist exact qualification authority in the caller's transaction.

    Managed Workday callers supply their stable operation id as
    ``authority_key``. A different operation can therefore make a later,
    identical request, while replay of the same operation is exact and a
    conflicting body fails closed. Legacy callers without an operation id
    retain task+specification idempotence.
    """

    from app.strategic_completion import extract_qualify_request

    normalized = extract_qualify_request(request)
    if normalized is None:
        return None
    version = (
        await session.execute(
            select(ReleaseVersion)
            .where(
                ReleaseVersion.project_id == project.id,
                ReleaseVersion.status == "open",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is None or version.phase != "stabilize":
        return None
    reason = normalized["reason"] or None
    probes = sorted(normalized["probes"])
    spec_hash = _specification_hash(
        project_id=project.id,
        release_version_id=version.id,
        source_task_id=task.id,
        reason=reason,
        probes=probes,
        expected_phase_changed_at=version.phase_changed_at,
    )
    if authority_key is None:
        normalized_authority = f"legacy-task-spec:{task.id}:{spec_hash}"
    else:
        normalized_authority = _normalize_authority_key(authority_key)
    idempotency_key = _authority_idempotency_key(
        project_id=project.id,
        authority_kind=authority_kind,
        authority_key=normalized_authority,
    )
    existing = (
        await session.execute(
            select(ReleaseQualificationIntent).where(
                ReleaseQualificationIntent.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.specification_hash != spec_hash
            or existing.authority_key != normalized_authority
            or existing.authority_kind != authority_kind
            or existing.release_version_id != version.id
        ):
            raise ValueError(
                "qualification intent authority was already used for a "
                "different request"
            )
        return existing
    intent = ReleaseQualificationIntent(
        project_id=project.id,
        release_version_id=version.id,
        source_task_id=task.id,
        authority_kind=authority_kind,
        authority_key=normalized_authority,
        idempotency_key=idempotency_key,
        specification_hash=spec_hash,
        reason=reason,
        requested_probes=probes,
        expected_phase_changed_at=version.phase_changed_at,
        status="pending",
        next_attempt_at=_utcnow(),
    )
    session.add(intent)
    await session.flush()
    return intent


def _settle_committed_result(
    intent: ReleaseQualificationIntent,
    *,
    now,
) -> None:
    """Project one exact committed delivery receipt onto intent lifecycle."""

    result = dict(intent.result or {})
    status = str(result.get("status") or "unknown")
    if status in _SUPERSEDED_RESULTS:
        intent.status = "superseded"
        intent.completed_at = now
    elif status in _FAILED_RESULTS:
        intent.status = "failed"
        intent.completed_at = now
        intent.last_error = str(result.get("error") or status)[:4000]
    elif status in _RETRYABLE_RESULTS:
        if int(intent.attempt_count or 0) >= QUALIFICATION_INTENT_MAX_ATTEMPTS:
            intent.status = "failed"
            intent.completed_at = now
            intent.last_error = str(result.get("error") or status)[:4000]
        else:
            intent.status = "pending"
            intent.next_attempt_at = now + timedelta(
                minutes=QUALIFICATION_INTENT_RETRY_MINUTES
            )
            # Preserve diagnostics while making the next claim an explicit,
            # classified retry rather than crash recovery.
            result["delivery_state"] = "retry_scheduled"
            intent.result = result
    elif status in _APPLIED_RESULTS:
        intent.status = "applied"
        intent.completed_at = now
    else:
        # New qualifier outcomes must be classified deliberately. Treating an
        # unknown string as success would permanently suppress redelivery while
        # hiding a contract drift between the qualifier and its durable outbox.
        intent.status = "failed"
        intent.completed_at = now
        intent.last_error = f"unexpected committed qualification status: {status}"[
            :4000
        ]
    intent.claim_id = None
    intent.lease_expires_at = None


def _receipt_identity(
    intent: ReleaseQualificationIntent,
    *,
    claim_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Canonical immutable identity for one committed delivery receipt."""

    effective_claim_id = claim_id if claim_id is not None else intent.claim_id
    return {
        "intent_id": str(intent.id),
        "project_id": str(intent.project_id),
        "release_version_id": str(intent.release_version_id),
        "source_task_id": str(intent.source_task_id),
        "authority_kind": intent.authority_kind,
        "authority_key": intent.authority_key,
        "idempotency_key": intent.idempotency_key,
        "specification_hash": intent.specification_hash,
        "attempt_count": int(intent.attempt_count or 0),
        "expected_phase_changed_at": (
            intent.expected_phase_changed_at.isoformat()
            if intent.expected_phase_changed_at is not None
            else None
        ),
        "claim_id": (
            str(effective_claim_id) if effective_claim_id is not None else None
        ),
    }


def _validate_committed_receipt(
    intent: ReleaseQualificationIntent,
) -> str | None:
    """Return a bounded diagnostic when a receipt is not this live claim's."""

    if not isinstance(intent.result, dict):
        return "qualification intent result payload is not an object"
    result = dict(intent.result)
    if result.get("delivery_state") != "committed":
        return "qualification receipt is not marked committed"
    if intent.status != "attempting":
        return "committed qualification receipt has no attempting intent"
    if intent.claim_id is None:
        return "committed qualification receipt has no live claim"
    if not isinstance(result.get("status"), str) or not result.get("status"):
        return "committed qualification receipt has no result status"
    if not isinstance(result.get("refresh_required"), bool):
        return "committed qualification receipt has invalid refresh_required"
    if result["refresh_required"] and result.get("status") != "ci_compliance_blocked":
        return (
            "committed qualification receipt refresh_required is only valid "
            "for ci_compliance_blocked"
        )
    if "refresh_delivery_state" in result:
        return "committed qualification receipt has premature refresh_delivery_state"
    for field, expected in _receipt_identity(intent).items():
        if field not in result:
            return f"committed qualification receipt identity missing: {field}"
        if field == "attempt_count" and type(result[field]) is not int:
            return "committed qualification receipt identity mismatch: attempt_count"
        if result.get(field) != expected:
            return f"committed qualification receipt identity mismatch: {field}"
    return None


def _fail_invalid_committed_receipt(
    intent: ReleaseQualificationIntent,
    *,
    error: str,
    now,
) -> None:
    """Terminalize an untrusted receipt without replaying or delivering it."""

    result = (
        dict(intent.result)
        if isinstance(intent.result, dict)
        else {"malformed_result_type": type(intent.result).__name__}
    )
    result["delivery_state"] = "invalid"
    result["receipt_validation_error"] = error[:500]
    result["invalidated_at"] = now.isoformat()
    intent.result = result
    intent.status = "failed"
    intent.completed_at = now
    intent.last_error = error[:4000]
    intent.claim_id = None
    intent.lease_expires_at = None
    intent.next_attempt_at = None


def _deliver_refresh_from_committed_receipt(
    intent: ReleaseQualificationIntent,
    project: Project,
) -> None:
    """Atomically arm a committed qualification wake under the Project lock."""

    if not isinstance(intent.result, dict):
        return
    result = dict(intent.result)
    if result.get("refresh_required") is not True:
        return
    if result.get("refresh_delivery_state") == "delivered":
        return
    from app.strategic_trigger import mark_strategic_refresh_in_vision

    vision = dict(project.vision or {})
    mark_strategic_refresh_in_vision(
        vision,
        "qualification blocked by actionable CI compliance evidence",
    )
    project.vision = vision
    flag_modified(project, "vision")
    result["refresh_delivery_state"] = "delivered"
    intent.result = result


async def _claim_intent(session, intent_id: uuid.UUID) -> uuid.UUID | None:
    snapshot = await session.get(ReleaseQualificationIntent, intent_id)
    if snapshot is None:
        return None
    project_id = snapshot.project_id
    version_id = snapshot.release_version_id
    project = (
        await session.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )
    ).scalar_one_or_none()
    version = (
        await session.execute(
            select(ReleaseVersion)
            .where(ReleaseVersion.id == version_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    intent = (
        await session.execute(
            select(ReleaseQualificationIntent)
            .where(ReleaseQualificationIntent.id == intent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None or version is None or intent is None:
        return None
    now = _utcnow()
    if intent.status in {"applied", "superseded", "failed"}:
        return None
    if not isinstance(intent.result, dict):
        _fail_invalid_committed_receipt(
            intent,
            error="qualification intent result payload is not an object",
            now=now,
        )
        session.add(intent)
        await session.commit()
        return None
    if (intent.result or {}).get("delivery_state") == "committed":
        # The release mutation and receipt committed together, but the worker
        # died before lifecycle settlement. Finalize from the receipt and never
        # replay the already-applied operation.
        receipt_error = _validate_committed_receipt(intent)
        if receipt_error is not None:
            _fail_invalid_committed_receipt(
                intent,
                error=receipt_error,
                now=now,
            )
        else:
            _settle_committed_result(intent, now=now)
            _deliver_refresh_from_committed_receipt(intent, project)
        session.add(project)
        session.add(intent)
        await session.commit()
        return None
    if (
        int(intent.attempt_count or 0) >= QUALIFICATION_INTENT_MAX_ATTEMPTS
        and (
            intent.status != "attempting"
            or intent.lease_expires_at is None
            or intent.lease_expires_at <= now
        )
    ):
        # A process can die after claiming but before settlement. Reclaiming an
        # expired lease must obey the same retry cap as an observed exception;
        # otherwise daemon crash loops can mint attempts 9, 10, ... forever.
        intent.status = "failed"
        intent.completed_at = now
        intent.last_error = (
            intent.last_error
            or "qualification delivery lease expired at retry cap"
        )[:4000]
        intent.claim_id = None
        intent.lease_expires_at = None
        session.add(intent)
        await session.commit()
        return None
    if project.paused:
        intent.status = "pending"
        intent.next_attempt_at = now + timedelta(
            minutes=QUALIFICATION_INTENT_RETRY_MINUTES
        )
        intent.claim_id = None
        intent.lease_expires_at = None
        session.add(intent)
        await session.commit()
        return None
    if (
        version.status != "open"
        or version.phase != "stabilize"
        or version.phase_changed_at != intent.expected_phase_changed_at
    ):
        intent.status = "superseded"
        intent.completed_at = now
        intent.result = {
            "delivery_state": "committed",
            "status": "superseded",
            "release_version_id": str(version.id),
            "release_version_status": version.status,
            "release_version_phase": version.phase,
            "expected_phase_changed_at": (
                intent.expected_phase_changed_at.isoformat()
                if intent.expected_phase_changed_at is not None
                else None
            ),
            "observed_phase_changed_at": (
                version.phase_changed_at.isoformat()
                if version.phase_changed_at is not None
                else None
            ),
        }
        intent.claim_id = None
        intent.lease_expires_at = None
        session.add(intent)
        await session.commit()
        return None
    if (
        intent.status == "attempting"
        and intent.lease_expires_at is not None
        and intent.lease_expires_at > now
    ):
        return None
    claim_id = uuid.uuid4()
    intent.status = "attempting"
    intent.claim_id = claim_id
    intent.attempt_count = int(intent.attempt_count or 0) + 1
    intent.lease_expires_at = now + timedelta(
        minutes=QUALIFICATION_INTENT_LEASE_MINUTES
    )
    intent.next_attempt_at = None
    intent.last_error = None
    session.add(intent)
    await session.commit()
    return claim_id


async def _heartbeat_claim(
    intent_id: uuid.UUID,
    claim_id: uuid.UUID,
    *,
    session_factory,
    stop: asyncio.Event,
) -> None:
    """Renew a live delivery lease without hiding a wedged worker forever."""

    while True:
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=QUALIFICATION_INTENT_HEARTBEAT_SECONDS
            )
            return
        except TimeoutError:
            pass
        try:
            async with session_factory() as session:
                intent = (
                    await session.execute(
                        select(ReleaseQualificationIntent)
                        .where(ReleaseQualificationIntent.id == intent_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    intent is None
                    or intent.status != "attempting"
                    or intent.claim_id != claim_id
                ):
                    return
                intent.lease_expires_at = _utcnow() + timedelta(
                    minutes=QUALIFICATION_INTENT_LEASE_MINUTES
                )
                session.add(intent)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "Qualification intent %s lease heartbeat failed",
                intent_id,
                exc_info=True,
            )


async def _settle_claim(
    intent_id: uuid.UUID,
    claim_id: uuid.UUID,
    *,
    session_factory,
    execution_error: BaseException | None,
) -> tuple[bool, dict[str, Any], uuid.UUID | None]:
    """CAS-settle a claim and return terminal state, receipt, and project id."""

    async with session_factory() as session:
        snapshot = await session.get(ReleaseQualificationIntent, intent_id)
        if snapshot is None:
            return False, {}, None
        project_id = snapshot.project_id
        version_id = snapshot.release_version_id
        project = (
            await session.execute(
                select(Project).where(Project.id == project_id).with_for_update()
            )
        ).scalar_one_or_none()
        version = (
            await session.execute(
                select(ReleaseVersion)
                .where(ReleaseVersion.id == version_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        intent = (
            await session.execute(
                select(ReleaseQualificationIntent)
                .where(ReleaseQualificationIntent.id == intent_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if project is None or version is None or intent is None:
            return False, {}, project_id
        if intent.status != "attempting" or intent.claim_id != claim_id:
            return (
                False,
                (
                    dict(intent.result)
                    if isinstance(intent.result, dict)
                    else {"malformed_result_type": type(intent.result).__name__}
                ),
                project_id,
            )
        now = _utcnow()
        if not isinstance(intent.result, dict):
            _fail_invalid_committed_receipt(
                intent,
                error="qualification intent result payload is not an object",
                now=now,
            )
            session.add(intent)
            await session.commit()
            return True, dict(intent.result or {}), project_id
        if (intent.result or {}).get("delivery_state") == "committed":
            # A commit acknowledgement can be lost after PostgreSQL made the
            # transaction durable. Only an exact claim-bound receipt is
            # authoritative; a mismatched result must never settle or wake.
            receipt_error = _validate_committed_receipt(intent)
            if receipt_error is not None:
                _fail_invalid_committed_receipt(
                    intent,
                    error=receipt_error,
                    now=now,
                )
            else:
                _settle_committed_result(intent, now=now)
                _deliver_refresh_from_committed_receipt(intent, project)
                session.add(project)
        elif execution_error is not None:
            intent.last_error = (
                f"{type(execution_error).__name__}: {execution_error}"
            )[:4000]
            if isinstance(execution_error, asyncio.CancelledError):
                # An orderly daemon shutdown rolls the transaction back and is
                # not a delivery attempt. Do not exhaust the durable failure
                # budget merely because the service was restarted.
                intent.attempt_count = max(0, int(intent.attempt_count or 0) - 1)
            if int(intent.attempt_count or 0) >= QUALIFICATION_INTENT_MAX_ATTEMPTS:
                intent.status = "failed"
                intent.completed_at = now
            else:
                intent.status = "pending"
                intent.next_attempt_at = (
                    now
                    if isinstance(execution_error, asyncio.CancelledError)
                    else now
                    + timedelta(minutes=QUALIFICATION_INTENT_RETRY_MINUTES)
                )
            intent.claim_id = None
            intent.lease_expires_at = None
        else:
            intent.status = "pending"
            intent.next_attempt_at = now + timedelta(
                minutes=QUALIFICATION_INTENT_RETRY_MINUTES
            )
            intent.last_error = "qualification delivery ended without a receipt"
            intent.claim_id = None
            intent.lease_expires_at = None
        session.add(intent)
        await session.commit()
        return (
            intent.status in {"applied", "superseded", "failed"},
            dict(intent.result or {}),
            project_id,
        )


class QualificationIntentClaimLost(RuntimeError):
    """The delivery worker no longer owns the durable claim."""


async def _commit_fenced_receipt(
    session,
    *,
    intent_id: uuid.UUID,
    claim_id: uuid.UUID,
    receipt: dict[str, Any],
) -> None:
    """CAS the claim immediately before committing release mutations."""

    # _apply_qualify_request may have accumulated new Task/Version rows. Lock
    # without flushing them first; losing the claim must roll back all of those
    # changes instead of leaking a side effect without its authority receipt.
    with session.no_autoflush:
        snapshot = await session.get(ReleaseQualificationIntent, intent_id)
        if snapshot is None:
            raise QualificationIntentClaimLost("qualification intent disappeared")
        project_state = (
            await session.execute(
                select(Project.id, Project.paused)
                .where(Project.id == snapshot.project_id)
                .with_for_update()
            )
        ).one_or_none()
        version_state = (
            await session.execute(
                select(
                    ReleaseVersion.id,
                    ReleaseVersion.status,
                    ReleaseVersion.phase,
                    ReleaseVersion.phase_changed_at,
                )
                .where(ReleaseVersion.id == snapshot.release_version_id)
                .with_for_update()
            )
        ).one_or_none()
        intent_state = (
            await session.execute(
                select(ReleaseQualificationIntent)
                .where(ReleaseQualificationIntent.id == intent_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    now = _utcnow()
    if (
        project_state is None
        or version_state is None
        or intent_state is None
        or intent_state.status != "attempting"
        or intent_state.claim_id != claim_id
        or intent_state.lease_expires_at is None
        or intent_state.lease_expires_at <= now
    ):
        raise QualificationIntentClaimLost(
            "qualification intent claim expired or was superseded before commit"
        )
    # Mutating paths acquire Project -> Version themselves, so a concurrent
    # pause/cut cannot cross their write. Read-only early outcomes do not; fold
    # any drift observed at this final fence into the exact durable receipt.
    if (
        version_state.status != "open"
        or version_state.phase != "stabilize"
        or version_state.phase_changed_at != intent_state.expected_phase_changed_at
    ):
        receipt = {
            **receipt,
            "status": "superseded",
            "release_version_status": version_state.status,
            "release_version_phase": version_state.phase,
        }
    elif project_state.paused and receipt.get("status") != "project_paused":
        receipt = {**receipt, "status": "project_paused"}
    receipt.pop("refresh_delivery_state", None)
    receipt["refresh_required"] = bool(
        receipt.get("status") == "ci_compliance_blocked"
        and receipt.get("refresh_required") is True
    )
    receipt = {
        **receipt,
        "delivery_state": "committed",
        **_receipt_identity(intent_state, claim_id=claim_id),
    }
    fenced_update = await session.execute(
        update(ReleaseQualificationIntent)
        .where(
            ReleaseQualificationIntent.id == intent_id,
            ReleaseQualificationIntent.status == "attempting",
            ReleaseQualificationIntent.claim_id == claim_id,
            ReleaseQualificationIntent.lease_expires_at > now,
        )
        .values(result=receipt, updated_at=now)
    )
    if fenced_update.rowcount != 1:
        raise QualificationIntentClaimLost(
            "qualification intent claim was lost during receipt fencing"
        )
    # Exact release mutation + exact delivery receipt are one commit.
    await session.commit()


async def _rollback_failed_execution(session) -> None:
    """Discard every failed apply mutation before this session is reused."""

    await session.rollback()
    # Production factories return independent sessions, but the SQLite test
    # harness deliberately reuses one identity map. SQLAlchemy can retain a
    # flag_modified JSON object as dirty after rollback in that arrangement;
    # refresh it from the rolled-back database before claim settlement commits.
    for obj in tuple(session.dirty):
        await session.refresh(obj)
    session.expire_all()


async def drain_qualification_intent(
    intent_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Claim and apply one intent without replaying a committed receipt."""

    from app.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    async with factory() as claim_session:
        claim_id = await _claim_intent(claim_session, intent_id)
    if claim_id is None:
        return False

    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_claim(
            intent_id,
            claim_id,
            session_factory=factory,
            stop=stop_heartbeat,
        ),
        name=f"qualification-intent-heartbeat:{intent_id}",
    )
    execution_error: BaseException | None = None
    execution_session = None
    refresh_required = False
    try:
        async with asyncio.timeout(QUALIFICATION_INTENT_EXECUTION_TIMEOUT_SECONDS):
            async with factory() as execution_session:
                intent = await execution_session.get(
                    ReleaseQualificationIntent, intent_id, populate_existing=True
                )
                if (
                    intent is None
                    or intent.status != "attempting"
                    or intent.claim_id != claim_id
                ):
                    raise QualificationIntentClaimLost(
                        "qualification intent claim was superseded"
                    )
                intent_receipt_fields = _receipt_identity(
                    intent,
                    claim_id=claim_id,
                )
                project = await execution_session.get(
                    Project, intent.project_id, populate_existing=True
                )
                version = await execution_session.get(
                    ReleaseVersion, intent.release_version_id, populate_existing=True
                )
                apply_result: dict[str, Any] = {}
                if project is None or version is None:
                    apply_result = {"status": "superseded"}
                elif project.paused:
                    apply_result = {"status": "project_paused"}
                elif (
                    version.status != "open"
                    or version.phase != "stabilize"
                    or version.phase_changed_at != intent.expected_phase_changed_at
                ):
                    apply_result = {
                        "status": "superseded",
                        "release_version_status": version.status,
                        "release_version_phase": version.phase,
                    }
                else:
                    source_task = await execution_session.get(
                        Task, intent.source_task_id
                    )
                    if source_task is None:
                        # The committed intent is the authority. Task retention
                        # may remove its source row, but must not invalidate the
                        # already-normalized release request.
                        source_task = SimpleNamespace(id=intent.source_task_id)
                    from app.strategic_completion import _apply_qualify_request

                    refresh_required = await _apply_qualify_request(
                        {
                            "reason": intent.reason,
                            "probes": list(intent.requested_probes or []),
                        },
                        source_task,
                        project,
                        execution_session,
                        path="durable_qualification_intent",
                        target_version=version,
                        expected_phase_changed_at=intent.expected_phase_changed_at,
                        exact_authority=True,
                        result_out=apply_result,
                    )
                if str(apply_result.get("status") or "unknown") in {
                    "error",
                    "migration_identity_deferred",
                    "project_paused",
                    "superseded",
                    "superseded_phase_epoch",
                    "task_admission_deferred",
                    "version_not_found",
                    "version_not_open",
                    "wrong_phase",
                }:
                    # These outcomes authorize no release mutation. Discard any
                    # compliance-cache/error projection accumulated before the
                    # final authority fence, then commit only the exact result
                    # receipt below in a fresh transaction.
                    await _rollback_failed_execution(execution_session)
                receipt = {
                    **apply_result,
                    "delivery_state": "committed",
                    **intent_receipt_fields,
                    "refresh_required": bool(refresh_required),
                    "committed_at": _utcnow().isoformat(),
                }
                await _commit_fenced_receipt(
                    execution_session,
                    intent_id=intent_id,
                    claim_id=claim_id,
                    receipt=receipt,
                )
    except asyncio.CancelledError as exc:
        execution_error = exc
        if execution_session is not None:
            await _rollback_failed_execution(execution_session)
    except Exception as exc:  # noqa: BLE001
        execution_error = exc
        if execution_session is not None:
            await _rollback_failed_execution(execution_session)
        logger.warning(
            "Qualification intent %s execution failed; retaining for retry",
            intent_id,
            exc_info=True,
        )
    finally:
        stop_heartbeat.set()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

    terminal, result, project_id = await _settle_claim(
        intent_id,
        claim_id,
        session_factory=factory,
        execution_error=execution_error,
    )

    if isinstance(execution_error, asyncio.CancelledError):
        raise execution_error
    return terminal


async def _due_intent_ids(*, session_factory, limit: int) -> list[uuid.UUID]:
    now = _utcnow()
    async with session_factory() as read_session:
        return list(
            (
                (
                    await read_session.execute(
                        select(ReleaseQualificationIntent.id)
                        .where(
                            or_(
                                (
                                    ReleaseQualificationIntent.status == "pending"
                                )
                                & or_(
                                    ReleaseQualificationIntent.next_attempt_at.is_(
                                        None
                                    ),
                                    ReleaseQualificationIntent.next_attempt_at <= now,
                                ),
                                (
                                    ReleaseQualificationIntent.status == "attempting"
                                )
                                & or_(
                                    ReleaseQualificationIntent.lease_expires_at.is_(
                                        None
                                    ),
                                    ReleaseQualificationIntent.lease_expires_at <= now,
                                ),
                            )
                        )
                        .order_by(ReleaseQualificationIntent.created_at.asc())
                        .limit(max(1, min(int(limit), 50)))
                    )
                )
                .scalars()
                .all()
            )
        )


async def drain_due_qualification_intents(
    *,
    session_factory=None,
    limit: int = 1,
) -> int:
    """Drain a bounded due batch; useful for tests and manual recovery."""

    from app.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    ids = await _due_intent_ids(session_factory=factory, limit=limit)
    processed = 0
    for intent_id in ids:
        if await drain_qualification_intent(intent_id, session_factory=factory):
            processed += 1
    return processed


def _background_drain_done(task: asyncio.Task) -> None:
    _background_drains.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Qualification intent background delivery was cancelled")
    except Exception:  # noqa: BLE001
        logger.exception("Qualification intent background delivery crashed")


async def launch_due_qualification_intents(
    *,
    session_factory=None,
    limit: int = 1,
) -> int:
    """Launch slow due delivery outside the recurring reaper watchdog path."""

    from app.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    available = max(0, min(int(limit), 1) - len(_background_drains))
    if available == 0:
        return 0
    ids = await _due_intent_ids(session_factory=factory, limit=available)
    for intent_id in ids:
        task = asyncio.create_task(
            drain_qualification_intent(intent_id, session_factory=factory),
            name=f"qualification-intent:{intent_id}",
        )
        _background_drains.add(task)
        task.add_done_callback(_background_drain_done)
    return len(ids)
