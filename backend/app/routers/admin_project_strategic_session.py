"""Strategic session and workbench helper endpoints for admin projects.

This router contains the strategic-session CRUD endpoints and the private
helpers that back them. Read-model helpers live in strategic_state_read_model
and work_session_check_in. It is registered in main.py alongside
admin_projects_router under the same /api/admin prefix.
"""

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import re
import time
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy import func, or_, and_
from typing import Dict, Any
import uuid
from sqlalchemy.orm.attributes import flag_modified

from app.database import AsyncSessionLocal, get_session
from app.broadcast import broadcast_to_project
from app.shared_state import limiter
from app.models import (
    MESSAGE_CONTENT_MAX,
    Alert,
    Artifact,
    CompletionDebtEntry,
    ComputeJob,
    ComputeJobStatus,
    Direction,
    CriterionEvidence,
    KnowledgeDocument,
    LocalRunner,
    MessagesLastSeen,
    MessagesNoisePrefs,
    ProjectMessage,
    ProjectMessageAttachmentUpload,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    ReleaseVersion,
    StrategicCycle,
    Task,
    TaskStatus,
    TaskWakeRegistration,
    Project,
    WakeDeliveryState,
    _utcnow,
)
from app.auth import get_current_user
from app.admin import _get_user_project_with_role
from app import strategic_cycle
from app.message_convergence import LINKED_STATUS, OPEN_STATUS
from app.operator_attention import build_operator_attention
from app.project_message_creation import (
    build_project_message,
    create_operator_project_message,
    lock_project_and_next_message_sequence,
    parse_operator_message_type,
)
from app.project_message_projection import (
    MESSAGE_EVENT_REFETCH_SURFACES,
    message_preview_dict,
)
from app.strategic_context_package import (
    build_strategic_context_package_summary,
    load_strategic_context_evidence,
    resolve_selected_ref_descriptors,
    selected_ref_descriptor,
)
from app.strategic_runtime_work import build_strategic_runtime_summary
# Canonical runner-liveness window (300s). is_live in _runner_health is purely
# time-derived (status=='active' AND now-last_seen < this), so the broad-revision
# fingerprint must probe it against the wall clock, not just max(last_seen_at).
from app.routers.admin_runners import _RUNNER_LIVENESS_SECONDS
from app.runner_project_binding import runner_allowed_for_project
from app.work_session_actions import build_work_session_actions
from app.work_session_command_execution import (
    OPERATOR_COMMAND_EVENT_TYPE,
    build_work_session_command_jobs,
    existing_work_session_command_result_for_idempotency_key,
    execute_work_session_command,
)
from app.work_session_commands import build_work_session_commands
from app.work_session_metrics import build_work_session_metric
from app.workbench_state import (
    WORKBENCH_STATE_MAX_BYTES,
    build_message_strategic_session_snapshot,
    get_strategic_workbench_state,
    persist_strategic_workbench_update,
    sanitize_strategic_session_snapshot,
    workbench_context_for_strategic_session_snapshot,
    workbench_state_size_bytes,
)
from app.workbench_events import (
    SURFACE_MESSAGES,
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    build_workbench_event_feed,
    emit_workbench_refetch,
)
from app.strategic_state_read_model import (
    _collect_selected_refs_for_resolution,
    _compact_text,
    _cycle_summary,
    _dedupe_workbench_refs,
    _direction_summary,
    _integrity_conflict_trend,
    _is_live_strategic_task,
    _problem_model_summary,
    _resident_session_available,
    _resident_session_availability,
    _resident_session_summary,
    _running_update_summary,
    _strategic_refresh_summary,
    _task_summary,
    _build_task_focus,
    compute_model_integrity_conflicts,
)
from app.work_session_check_in import (
    _CHECK_IN_EVENT_TYPE,
    _build_work_session_check_in_projection,
    _message_is_operator_check_in,
    _persist_strategic_check_in_marker,
)
from app.server_timing import async_timed
from app.shared_state import runtime_config
from app.storage import (
    ARTIFACT_BUCKET_NAME,
    GCP_PROJECT_ID,
    _generate_v4_signed_url,
    delete_storage_uri_best_effort,
    generate_download_signed_url,
    storage as gcs_storage,
)
from app.strategic_resident_session import (
    STRATEGIC_SESSION_ENABLED_SETTING,
    build_resident_runner_options,
    start_resident_session,
    stop_resident_session,
    sync_resident_session_for_interaction_mode,
)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-projects"],
    dependencies=[Depends(get_current_user)],
)

logger = logging.getLogger(__name__)

# In-process TTL cache for _resolve_candidate_currency results.
# Key: (project_id_str, integration_branch, candidate_sha)
# Value: (result_dict, expires_monotonic)
_CURRENCY_CACHE: dict[tuple[str, str, str], tuple[dict, float]] = {}
_CURRENCY_CACHE_TTL_S = 60.0
# Holds strong references to background currency-resolve tasks so they are not
# garbage-collected before they complete. Entries are discarded on completion.
_BG_TASKS: set[asyncio.Task] = set()

# In-process TTL cache for project cumulative cost aggregation.
# Key: project_id_str
# Value: (cost_float, expires_monotonic)
_COST_CACHE: dict[str, tuple[float, float]] = {}
_COST_CACHE_TTL_S = 90.0

# Phase-1 fingerprint constants for the broad-revision assembly-skip guard.
# These mirror the "active" filters the Phase-2 assembly applies so a single
# indexed count/max scalar can stand in for the expensive multi-row build when
# nothing in a segment changed. Kept local (rather than importing the private
# tuples from strategic_runtime_work) so the fingerprint contract is explicit
# and testable at this seam.
_FINGERPRINT_ACTIVE_COMPUTE_JOB_STATUSES = (
    ComputeJobStatus.QUEUED,
    ComputeJobStatus.STARTING,
    ComputeJobStatus.RUNNING,
    ComputeJobStatus.POLLING,
)
_FINGERPRINT_ACTIVE_WAKE_STATES = (
    WakeDeliveryState.PENDING,
    WakeDeliveryState.FIRED,
)

async def _currency_resolve_and_cache(
    project_id: uuid.UUID,
    last_qual: dict,
    integration_branch: str,
    cache_key: tuple[str, str, str],
) -> None:
    """Resolve candidate currency off the request path and populate _CURRENCY_CACHE.

    Opens its own short-lived session so it is independent of the
    request-scoped session that closes when the response is sent.
    Failure is logged and silently swallowed — a background resolution
    error must never surface to the caller.
    api_unreachable verdicts are not cached (transient GitHub blip should
    not pin a stale banner for the full TTL).
    """
    from app.strategic_spawning import _resolve_candidate_currency

    try:
        async with AsyncSessionLocal() as bg_session:
            _proj = (
                await bg_session.execute(select(Project).where(Project.id == project_id))
            ).scalars().first()
            if _proj is None:
                return
            currency = await _resolve_candidate_currency(
                bg_session, _proj, last_qual, integration_branch
            )
        _verdict = currency.get("verdict") if isinstance(currency, dict) else None
        if _verdict != "api_unreachable":
            _now = time.monotonic()
            _expired = [k for k, v in _CURRENCY_CACHE.items() if v[1] <= _now]
            for _k in _expired:
                del _CURRENCY_CACHE[_k]
            _CURRENCY_CACHE[cache_key] = (currency, _now + _CURRENCY_CACHE_TTL_S)
    except Exception:
        logger.exception(
            "[STRATEGIC_STATE] background currency resolve failed for project %s",
            project_id,
        )


def _currency_cache_fingerprint(
    project: Project, open_version: ReleaseVersion | None
) -> str:
    """Fingerprint the in-memory currency-cache state that feeds sha_freshness.

    sha_freshness is resolved out-of-band into ``_CURRENCY_CACHE`` (never the DB)
    by ``_currency_resolve_and_cache``, so the DB-derived fingerprints cannot see
    the cold→warm verdict transition: a cold-cache 200 (verdict='unknown') warms
    the cache, and with identical DB state the next poll would otherwise match the
    broad revision and 304 BEFORE assembly — pinning the operator's banner on
    'unknown'. Folding this cache-state string into the broad revision makes that
    matching-token 304 impossible across the transition, so the two-poll cold→warm
    currency design still delivers the resolved verdict (exactly as the pre-skip
    full-body ETag did).

    Read-only: mirrors the request-path cache read below (same key + monotonic TTL
    check) but never spawns the background resolve. Any failure degrades to a
    constant so this segment simply falls back to pre-fingerprint behavior rather
    than sinking the endpoint.
    """
    if open_version is None:
        return "no-open-version"
    try:
        from app.strategic_spawning import get_project_integration_branch

        _last_qual = open_version.last_qualification or {}
        _integration_branch = get_project_integration_branch(project)
        _candidate_sha = _last_qual.get("candidate_sha") or ""
        _cache_key = (str(project.id), _integration_branch, _candidate_sha)
        _cached = _CURRENCY_CACHE.get(_cache_key)
        if _cached is None or _cached[1] <= time.monotonic():
            # Cold / expired: assembly will serve an 'unknown' block and spawn a
            # background resolve. When that resolve warms the cache, this string
            # flips warm → a new revision → assembly delivers the real verdict.
            return f"cold:{_candidate_sha}"
        _verdict = (
            _cached[0].get("verdict") if isinstance(_cached[0], dict) else None
        )
        return f"warm:{_candidate_sha}:{_verdict}"
    except Exception:
        logger.exception(
            "[STRATEGIC_STATE] currency-cache fingerprint failed for project %s",
            project.id,
        )
        return "error"


def _build_work_session_summary(
    project: Project,
    *,
    workbench_state: Dict[str, Any],
    context_package: Dict[str, Any] | None,
    selected_ref_descriptors: dict[tuple[str, str], Dict[str, Any]] | None,
    attention: Dict[str, Any] | None,
    runtime_summary: Dict[str, Any] | None,
    message_counts: Dict[str, int],
    task_counts: Dict[str, int],
) -> Dict[str, Any]:
    selected_refs = _dedupe_workbench_refs(workbench_state.get("selected_refs"))
    attention_counts = (
        attention.get("counts", {}) if isinstance(attention, dict) else {}
    )
    context_sources = (
        context_package.get("sources", [])
        if isinstance(context_package, dict)
        else []
    )
    context_item_count = (
        int(context_package.get("item_count") or 0)
        if isinstance(context_package, dict)
        else 0
    )
    context_status = (
        _compact_text(context_package.get("status"), max_chars=40)
        if isinstance(context_package, dict)
        else None
    )
    context_updated_at = (
        _compact_text(context_package.get("updated_at"), max_chars=80)
        if isinstance(context_package, dict)
        else None
    )
    runtime_work = (
        runtime_summary.get("work", {}) if isinstance(runtime_summary, dict) else {}
    )
    local_runner = (
        runtime_summary.get("local_runner", {})
        if isinstance(runtime_summary, dict)
        else {}
    )
    compute_jobs = (
        runtime_work.get("compute_jobs", {}) if isinstance(runtime_work, dict) else {}
    )
    compute_counts = (
        compute_jobs.get("counts", {}) if isinstance(compute_jobs, dict) else {}
    )
    compute_spend = (
        compute_jobs.get("spend", {}) if isinstance(compute_jobs, dict) else {}
    )
    compute_window_minutes = (
        int(compute_jobs.get("window_minutes"))
        if isinstance(compute_jobs, dict)
        and isinstance(compute_jobs.get("window_minutes"), (int, float))
        and compute_jobs.get("window_minutes") > 0
        else None
    )
    held_approvals = (
        runtime_work.get("held_approvals", {})
        if isinstance(runtime_work, dict)
        else {}
    )
    local_runner_holds = (
        runtime_work.get("local_runner_holds", {})
        if isinstance(runtime_work, dict)
        else {}
    )
    parked_wakes = (
        runtime_work.get("parked_wakes", {}) if isinstance(runtime_work, dict) else {}
    )
    wake_counts = (
        parked_wakes.get("counts", {}) if isinstance(parked_wakes, dict) else {}
    )
    held_approval_count = (
        int(held_approvals.get("count") or 0)
        if isinstance(held_approvals, dict)
        else 0
    )
    local_runner_hold_count = (
        int(local_runner_holds.get("count") or 0)
        if isinstance(local_runner_holds, dict)
        else 0
    )
    eligible_live_runner_count = (
        int(local_runner.get("eligible_live_count") or 0)
        if isinstance(local_runner, dict)
        else 0
    )
    local_continuation = (
        local_runner.get("continuation", {})
        if isinstance(local_runner, dict)
        else {}
    )
    continuation_needs_runner = bool(
        isinstance(local_continuation, dict)
        and local_continuation.get("enabled")
        and local_continuation.get("fresh")
        and eligible_live_runner_count == 0
        and local_runner.get("mode") != "local"
    )

    session_started_at = workbench_state.get("session_started_at")
    session_id = f"strategic-session:{project.id}:{session_started_at or 'idle'}"
    metrics = [
        build_work_session_metric(
            "attention.needs_operator",
            int(attention_counts.get("needs_operator") or 0),
            tone="warning"
            if int(attention_counts.get("needs_operator") or 0) > 0
            else "neutral",
        ),
        build_work_session_metric(
            "attention.watch",
            int(attention_counts.get("watch") or 0),
            tone="watch" if int(attention_counts.get("watch") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "messages.open",
            int(message_counts.get("open") or 0),
        ),
        build_work_session_metric(
            "messages.linked",
            int(message_counts.get("linked") or 0),
        ),
        build_work_session_metric(
            "tasks.running",
            int(task_counts.get("running") or 0),
            tone="active" if int(task_counts.get("running") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "tasks.blocked",
            int(task_counts.get("blocked") or 0),
            tone="warning" if int(task_counts.get("blocked") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "tasks.parked",
            int(task_counts.get("parked") or 0),
            tone="watch" if int(task_counts.get("parked") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "tasks.failed",
            int(task_counts.get("failed") or 0),
            tone="critical" if int(task_counts.get("failed") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "runtime.compute_active",
            int(compute_counts.get("active") or 0),
            tone="active" if int(compute_counts.get("active") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "runtime.compute_failed",
            int(compute_counts.get("failed_recent") or 0),
            tone="critical"
            if int(compute_counts.get("failed_recent") or 0) > 0
            else "neutral",
            provenance_extras={"window_minutes": compute_window_minutes},
        ),
        build_work_session_metric(
            "runtime.held_approvals",
            held_approval_count,
            tone="warning" if held_approval_count > 0 else "neutral",
        ),
        build_work_session_metric(
            "runtime.local_strategic_ready",
            eligible_live_runner_count,
            tone=(
                "active"
                if eligible_live_runner_count > 0
                else "warning"
                if continuation_needs_runner
                else "neutral"
            ),
        ),
        build_work_session_metric(
            "runtime.local_runner_holds",
            local_runner_hold_count,
            tone="warning" if local_runner_hold_count > 0 else "neutral",
        ),
        build_work_session_metric(
            "runtime.parked_wakes",
            int(wake_counts.get("active") or 0),
            tone="watch" if int(wake_counts.get("active") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "runtime.wake_problems",
            int(wake_counts.get("problem") or 0),
            tone="critical" if int(wake_counts.get("problem") or 0) > 0 else "neutral",
        ),
        build_work_session_metric(
            "runtime.compute_spend",
            float(compute_spend.get("window_total_usd") or 0.0),
            tone="watch"
            if float(compute_spend.get("window_total_usd") or 0.0) > 0
            else "neutral",
            provenance_extras={"window_minutes": compute_window_minutes},
        ),
        build_work_session_metric(
            "context.stale",
            1 if context_status == "stale" else 0,
            tone="warning" if context_status == "stale" else "neutral",
            provenance_extras={
                "transform": "equals:stale",
                "status": context_status,
                "updated_at": context_updated_at,
            },
        ),
        build_work_session_metric(
            "context.items",
            context_item_count,
            provenance_extras={
                "status": context_status,
                "updated_at": context_updated_at,
            },
        ),
    ]
    actions = build_work_session_actions(
        attention=attention,
        runtime_work=runtime_work,
        context_status=context_status,
        local_runner=local_runner if isinstance(local_runner, dict) else None,
    )
    commands = build_work_session_commands(
        project_id=project.id,
        actions=actions,
        workbench_state=workbench_state,
        context_package=context_package,
        local_runner=local_runner if isinstance(local_runner, dict) else None,
        attention=attention,
    )
    return {
        "version": 1,
        "id": session_id[:240],
        "mode": workbench_state.get("interaction_mode") or "chat",
        "active_surface": workbench_state.get("active_surface") or "strategic",
        "session_started_at": session_started_at,
        "last_interaction_at": workbench_state.get("last_interaction_at"),
        "context_package_id": context_package.get("id")
        if isinstance(context_package, dict)
        else None,
        "attention_level": attention.get("level")
        if isinstance(attention, dict)
        else "clear",
        "selected_refs": selected_refs,
        "selected_ref_descriptors": [
            selected_ref_descriptor(ref, context_package, selected_ref_descriptors)
            for ref in selected_refs[:12]
            if isinstance(ref, dict)
        ],
        "curated_metrics": metrics,
        "actions": actions,
        "commands": commands,
        "context_sources": context_sources[:8] if isinstance(context_sources, list) else [],
    }


class StrategicSessionUpdate(BaseModel):
    workbench: Dict[str, Any] = Field(default_factory=dict)


class StrategicSessionMessageCreate(BaseModel):
    content: str
    message_type: str = "NOTE"
    context: Dict[str, Any] | None = None
    workbench_context: Dict[str, Any] | None = None
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    reply_to_message_id: uuid.UUID | None = None
    source_refs: list[Dict[str, Any]] = Field(default_factory=list, max_length=12)
    artifact_refs: list[Dict[str, Any]] = Field(default_factory=list, max_length=12)
    attachments: list[Dict[str, Any]] = Field(default_factory=list, max_length=12)
    operator_intent: Dict[str, Any] | None = None

    @field_validator("idempotency_key")
    @classmethod
    def _nonblank_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key must not be blank")
        return normalized


class StrategicSessionCheckInCreate(BaseModel):
    content: str | None = None
    kind: str = "manual"
    workbench_context: Dict[str, Any] | None = None
    idempotency_key: str | None = None


class StrategicSessionCommandExecute(BaseModel):
    idempotency_key: str
    observed_at: int | None = None


class StrategicResidentSessionStart(BaseModel):
    runner_id: str | None = None


_ATTACHMENT_DEFAULT_CONTENT_TYPE = "application/octet-stream"
_ATTACHMENT_DEFAULT_FILENAME = "attachment"
_ATTACHMENT_FILENAME_MAX_CHARS = 200
_ATTACHMENT_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ATTACHMENT_CONTENT_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$"
)
_MESSAGE_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024
_MESSAGE_ATTACHMENT_UPLOAD_EXPIRY_MINUTES = 15
_MESSAGE_ATTACHMENT_CLEANUP_BATCH = 8
_MESSAGE_ATTACHMENT_CLEANUP_TIMEOUT_SECONDS = 5.0


def _sanitize_message_attachment_filename(filename: str) -> str:
    """Return a single safe GCS object-name segment for an uploaded attachment."""
    raw = (filename or "").strip().replace("\\", "/")
    name = raw.rsplit("/", 1)[-1].strip()
    name = _ATTACHMENT_SAFE_FILENAME_RE.sub("_", name).strip("._")
    if not name:
        return _ATTACHMENT_DEFAULT_FILENAME
    if len(name) <= _ATTACHMENT_FILENAME_MAX_CHARS:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and 1 <= len(ext) <= 32:
        keep = max(1, _ATTACHMENT_FILENAME_MAX_CHARS - len(dot) - len(ext))
        return f"{stem[:keep]}{dot}{ext}"
    return name[:_ATTACHMENT_FILENAME_MAX_CHARS]


def _attachment_content_type(content_type: str | None) -> str:
    """Validate the Content-Type header that will be bound into the signed PUT."""
    value = (content_type or "").strip()
    if not value:
        return _ATTACHMENT_DEFAULT_CONTENT_TYPE
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise HTTPException(status_code=400, detail="Invalid content_type")
    if not _ATTACHMENT_CONTENT_TYPE_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid content_type")
    return value


def _message_context_has_attachments(context: Dict[str, Any] | None) -> bool:
    raw = context.get("attachments") if isinstance(context, dict) else None
    return isinstance(raw, list) and any(isinstance(item, dict) for item in raw)


def _message_ref_uuid(value: Any, *, prefix: str | None = None) -> uuid.UUID | None:
    raw = str(value or "").strip()
    if prefix and raw.startswith(f"{prefix}:"):
        raw = raw.split(":", 1)[1]
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError):
        return None


_VISIBLE_MESSAGE_RECORD_KEYS = {
    "source_refs": frozenset({
        "id", "message_id", "task_id", "artifact_id", "tool_id", "project_id",
        "kind", "label", "title", "excerpt", "revision", "checksum", "author",
        "timestamp", "created_at", "updated_at",
    }),
    "artifact_refs": frozenset({
        "id", "artifact_id", "tool_id", "project_id", "kind", "label", "title",
        "revision", "checksum", "author", "timestamp", "created_at", "updated_at",
    }),
    "attachments": frozenset({
        "attachment_id", "storage_uri", "kind", "type", "mime_type", "media_type",
        "name", "filename", "size_bytes", "checksum",
    }),
    "operator_intent": frozenset({
        "kind", "action", "lane_owner", "summary", "target", "priority",
        "approval", "operation_id", "expected_revision",
    }),
}


def _bounded_visible_message_record(kind: str, item: Dict[str, Any]) -> Dict[str, Any]:
    allowed = _VISIBLE_MESSAGE_RECORD_KEYS[kind]
    result: Dict[str, Any] = {}
    for key, value in item.items():
        if key not in allowed:
            continue
        if isinstance(value, str):
            result[key] = value[:2_000]
        elif value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif key == "target" and isinstance(value, dict):
            result[key] = {
                str(target_key)[:80]: (
                    target_value[:500]
                    if isinstance(target_value, str)
                    else target_value
                )
                for target_key, target_value in list(value.items())[:12]
                if target_value is None or isinstance(target_value, (str, bool, int, float))
            }
    return result


def _assert_reference_revision(
    ref: Dict[str, Any] | None,
    *,
    revision_values: tuple[Any, ...] = (),
    checksum_value: Any = None,
    detail: str,
) -> None:
    if not ref:
        return
    requested_revision = ref.get("revision")
    if requested_revision is not None:
        valid_revisions = {
            str(value)
            for value in revision_values
            if value is not None
        }
        if str(requested_revision) not in valid_revisions:
            raise HTTPException(status_code=409, detail=f"{detail} revision changed")
    requested_checksum = ref.get("checksum")
    if requested_checksum is not None and str(requested_checksum) != str(checksum_value or ""):
        raise HTTPException(status_code=409, detail=f"{detail} checksum changed")


def _canonical_message_attachment(
    item: Dict[str, Any], project_id: uuid.UUID
) -> Dict[str, Any]:
    attachment_id = _message_ref_uuid(item.get("attachment_id") or item.get("id"))
    storage_uri = str(item.get("storage_uri") or "").strip()
    expected_prefix = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project_id}/{attachment_id}/"
        if attachment_id is not None
        else ""
    )
    object_tail = storage_uri[len(expected_prefix):] if expected_prefix else ""
    if (
        attachment_id is None
        or not storage_uri.startswith(expected_prefix)
        or not object_tail
        or "/" in object_tail
    ):
        raise HTTPException(status_code=400, detail="Invalid attachment reference")
    normalized = _bounded_visible_message_record("attachments", item)
    normalized["attachment_id"] = str(attachment_id)
    normalized["storage_uri"] = storage_uri
    return normalized


async def _consume_message_attachment_uploads(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_user_id: str,
    message_id: uuid.UUID,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    raw_attachments = context.get("attachments")
    if not isinstance(raw_attachments, list) or not raw_attachments:
        return context

    attachment_ids = {
        parsed
        for item in raw_attachments
        if isinstance(item, dict)
        and (parsed := _message_ref_uuid(item.get("attachment_id") or item.get("id")))
        is not None
    }
    receipts = (
        await session.execute(
            select(ProjectMessageAttachmentUpload)
            .where(ProjectMessageAttachmentUpload.attachment_id.in_(attachment_ids))
            .with_for_update()
        )
    ).scalars().all()
    by_id = {receipt.attachment_id: receipt for receipt in receipts}
    now = _utcnow()
    canonical: list[Dict[str, Any]] = []
    for item in raw_attachments:
        attachment_id = _message_ref_uuid(item.get("attachment_id") or item.get("id"))
        receipt = by_id.get(attachment_id) if attachment_id is not None else None
        if (
            receipt is None
            or receipt.project_id != project_id
            or receipt.actor_user_id != actor_user_id
            or receipt.storage_uri != str(item.get("storage_uri") or "").strip()
        ):
            raise HTTPException(status_code=400, detail="Invalid attachment upload receipt")
        if receipt.consumed_at is not None or receipt.message_id is not None:
            raise HTTPException(status_code=409, detail="Attachment upload already consumed")
        if receipt.expires_at <= now:
            raise HTTPException(status_code=409, detail="Attachment upload expired; upload again")

        normalized = _bounded_visible_message_record("attachments", item)
        normalized.update({
            "attachment_id": str(receipt.attachment_id),
            "storage_uri": receipt.storage_uri,
            "name": receipt.filename,
            "filename": receipt.filename,
            "mime_type": receipt.content_type or _ATTACHMENT_DEFAULT_CONTENT_TYPE,
            "size_bytes": receipt.expected_size_bytes,
        })
        canonical.append(normalized)
        receipt.consumed_at = now
        receipt.message_id = message_id
        session.add(receipt)
    return {**context, "attachments": canonical}


async def _cleanup_expired_message_attachment_uploads(
    project_id: uuid.UUID,
    actor_user_id: str,
) -> None:
    """Best-effort bounded orphan cleanup, run after upload responses."""

    try:
        async with AsyncSessionLocal() as cleanup_session:
            receipts = (
                await cleanup_session.execute(
                    select(ProjectMessageAttachmentUpload)
                    .where(
                        ProjectMessageAttachmentUpload.project_id == project_id,
                        ProjectMessageAttachmentUpload.actor_user_id == actor_user_id,
                        ProjectMessageAttachmentUpload.consumed_at.is_(None),
                        ProjectMessageAttachmentUpload.message_id.is_(None),
                        ProjectMessageAttachmentUpload.expires_at <= _utcnow(),
                    )
                    .order_by(ProjectMessageAttachmentUpload.expires_at.asc())
                    .limit(_MESSAGE_ATTACHMENT_CLEANUP_BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            try:
                cleanup_results = await asyncio.wait_for(
                    asyncio.gather(*(
                        delete_storage_uri_best_effort(receipt.storage_uri)
                        for receipt in receipts
                    )),
                    timeout=_MESSAGE_ATTACHMENT_CLEANUP_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await cleanup_session.rollback()
                return
            for receipt, deleted in zip(receipts, cleanup_results, strict=True):
                if deleted:
                    await cleanup_session.delete(receipt)
            await cleanup_session.commit()
    except Exception:
        logger.warning(
            "[ATTACHMENT] expired upload cleanup failed for project %s",
            project_id,
            exc_info=True,
        )


async def _validated_message_scoped_context(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    payload: StrategicSessionMessageCreate,
    lock_references: bool = False,
) -> tuple[Dict[str, Any], ProjectMessage | None]:
    """Build visible, bounded context and reject cross-project references."""

    context: Dict[str, Any] = {}
    visible_groups = {
        "source_refs": payload.source_refs,
        "artifact_refs": payload.artifact_refs,
        "attachments": payload.attachments,
    }
    for key, explicit_items in visible_groups.items():
        # Conversation context is message-scoped and visible. Never fall back
        # to the legacy free-form context object: an explicit empty chip list
        # must remain empty rather than silently attaching hidden state.
        items = explicit_items or []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise HTTPException(status_code=400, detail=f"{key} must be a list of objects")
        if len(items) > 12:
            raise HTTPException(status_code=400, detail=f"{key} exceeds 12 items")
        normalized: list[Dict[str, Any]] = []
        for item in items:
            item_project_id = item.get("project_id")
            if item_project_id is not None and str(item_project_id) != str(project_id):
                raise HTTPException(status_code=400, detail=f"Cross-project {key} reference")
            normalized_item = _bounded_visible_message_record(key, item)
            if key == "attachments":
                normalized_item = _canonical_message_attachment(item, project_id)
            normalized.append(normalized_item)
        if normalized:
            context[key] = normalized

    if payload.operator_intent is not None:
        context["operator_intent"] = _bounded_visible_message_record(
            "operator_intent", payload.operator_intent
        )

    reply_target: ProjectMessage | None = None
    if payload.reply_to_message_id is not None:
        reply_stmt = select(ProjectMessage).where(
            ProjectMessage.id == payload.reply_to_message_id
        )
        if lock_references:
            reply_stmt = reply_stmt.with_for_update()
        reply_target = (await session.execute(reply_stmt)).scalar_one_or_none()
        if reply_target is None or reply_target.project_id != project_id:
            raise HTTPException(status_code=400, detail="Invalid reply_to_message_id")
        context["reply_preview"] = {
            "id": str(reply_target.id),
            "from_type": reply_target.from_type,
            "actor_user_id": getattr(reply_target, "actor_user_id", None),
            "sequence": getattr(reply_target, "sequence", None),
            "content": _compact_text(reply_target.content, max_chars=240) or "",
        }

    # Validate project-owned references in three bounded set queries rather
    # than one round trip per chip (up to 24 chips are accepted).
    message_ids: set[uuid.UUID] = set()
    task_ids: set[uuid.UUID] = set()
    artifact_ids: set[uuid.UUID] = set()
    direction_ids: set[str] = set()
    runner_ids: set[uuid.UUID] = set()
    knowledge_ids: set[uuid.UUID] = set()
    cycle_ids: set[uuid.UUID] = set()
    reference_claims: dict[tuple[str, str], Dict[str, Any]] = {}
    seen_reference_identities: set[tuple[str, str]] = set()

    def _record_reference_claim(
        kind: str,
        ref_id: Any,
        ref: Dict[str, Any],
    ) -> None:
        identity = (kind, str(ref_id))
        if identity in seen_reference_identities:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate {kind} reference",
            )
        seen_reference_identities.add(identity)
        reference_claims[identity] = ref

    for ref in context.get("source_refs") or []:
        kind = str(ref.get("kind") or "").lower()
        ref_id = ref.get("id") or ref.get("message_id") or ref.get("task_id")
        if kind == "message":
            parsed = _message_ref_uuid(ref_id, prefix="message")
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid message source reference")
            message_ids.add(parsed)
            _record_reference_claim(kind, parsed, ref)
        elif kind == "task":
            parsed = _message_ref_uuid(ref_id, prefix="task")
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid task source reference")
            task_ids.add(parsed)
            _record_reference_claim(kind, parsed, ref)
        elif kind == "artifact":
            parsed = _message_ref_uuid(ref_id, prefix="artifact")
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid artifact source reference")
            artifact_ids.add(parsed)
            _record_reference_claim(kind, parsed, ref)
        elif kind == "canvas":
            if str(ref_id or "") != f"strategic-canvas:{project_id}":
                raise HTTPException(status_code=400, detail="Invalid canvas source reference")
            _record_reference_claim(kind, ref_id, ref)
        elif kind == "direction":
            raw_direction = str(ref_id or "").strip()
            if raw_direction.startswith("direction:"):
                raw_direction = raw_direction.split(":", 1)[1]
            if not raw_direction:
                raise HTTPException(status_code=400, detail="Invalid direction source reference")
            direction_ids.add(raw_direction)
            _record_reference_claim(kind, raw_direction, ref)
        elif kind == "runner":
            parsed = _message_ref_uuid(ref_id, prefix="runner")
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid runner source reference")
            runner_ids.add(parsed)
            _record_reference_claim(kind, parsed, ref)
        elif kind == "knowledge_document":
            parsed = _message_ref_uuid(ref_id, prefix="knowledge")
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid knowledge source reference")
            knowledge_ids.add(parsed)
            _record_reference_claim(kind, parsed, ref)
        elif kind == "strategic_cycle":
            parsed = _message_ref_uuid(ref_id, prefix="cycle")
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid cycle source reference")
            cycle_ids.add(parsed)
            _record_reference_claim(kind, parsed, ref)
        else:
            raise HTTPException(status_code=400, detail="Unsupported source reference kind")

    for ref in context.get("artifact_refs") or []:
        artifact_id = ref.get("artifact_id") or ref.get("id")
        parsed = _message_ref_uuid(artifact_id, prefix="artifact")
        if parsed is None:
            raise HTTPException(status_code=400, detail="Invalid artifact reference")
        artifact_ids.add(parsed)
        _record_reference_claim("artifact", parsed, ref)

    if message_ids:
        stmt = select(ProjectMessage).where(
            ProjectMessage.id.in_(message_ids),
            ProjectMessage.project_id == project_id,
        )
        if lock_references:
            stmt = stmt.with_for_update()
        rows = (await session.execute(stmt)).scalars().all()
        valid_ids = {row.id for row in rows}
        if valid_ids != message_ids:
            raise HTTPException(status_code=400, detail="Invalid message source reference")
        for row in rows:
            _assert_reference_revision(
                reference_claims.get(("message", str(row.id))),
                revision_values=(row.sequence, row.updated_at.isoformat() if row.updated_at else None),
                detail="Message source",
            )
    if task_ids:
        stmt = select(Task).where(
            Task.id.in_(task_ids),
            Task.project_id == project_id,
        )
        if lock_references:
            stmt = stmt.with_for_update()
        rows = (await session.execute(stmt)).scalars().all()
        valid_ids = {row.id for row in rows}
        if valid_ids != task_ids:
            raise HTTPException(status_code=400, detail="Invalid task source reference")
        for row in rows:
            _assert_reference_revision(
                reference_claims.get(("task", str(row.id))),
                revision_values=(row.updated_at.isoformat() if row.updated_at else None,),
                detail="Task source",
            )
    if artifact_ids:
        stmt = (
            select(Artifact)
            .join(Task, Task.id == Artifact.task_id)
            .where(Artifact.id.in_(artifact_ids), Task.project_id == project_id)
        )
        if lock_references:
            stmt = stmt.with_for_update()
        rows = (await session.execute(stmt)).scalars().all()
        valid_ids = {row.id for row in rows}
        if valid_ids != artifact_ids:
            raise HTTPException(status_code=400, detail="Invalid artifact reference")
        for row in rows:
            _assert_reference_revision(
                reference_claims.get(("artifact", str(row.id))),
                revision_values=(row.iteration, row.created_at.isoformat() if row.created_at else None),
                checksum_value=row.commit_hash,
                detail="Artifact",
            )
    for ids, model, detail in (
        (runner_ids, LocalRunner, "Invalid runner source reference"),
        (knowledge_ids, KnowledgeDocument, "Invalid knowledge source reference"),
        (cycle_ids, StrategicCycle, "Invalid cycle source reference"),
    ):
        if not ids:
            continue
        query_ids = {str(value) for value in ids} if model is StrategicCycle else ids
        stmt = select(model).where(
            model.id.in_(query_ids), model.project_id == project_id
        )
        if lock_references:
            stmt = stmt.with_for_update()
        rows = (await session.execute(stmt)).scalars().all()
        valid_ids = (
            {_message_ref_uuid(row.id) for row in rows}
            if model is StrategicCycle
            else {row.id for row in rows}
        )
        if valid_ids != ids:
            raise HTTPException(status_code=400, detail=detail)
        if model is KnowledgeDocument:
            for row in rows:
                _assert_reference_revision(
                    reference_claims.get(("knowledge_document", str(row.id))),
                    revision_values=(row.updated_at.isoformat() if row.updated_at else None,),
                    detail="Knowledge source",
                )
    if direction_ids:
        stmt = select(Direction).where(
            Direction.project_id == project_id,
            Direction.direction_key.in_(direction_ids),
        )
        if lock_references:
            stmt = stmt.with_for_update()
        rows = (await session.execute(stmt)).scalars().all()
        valid_direction_ids = {row.direction_key for row in rows}
        if set(valid_direction_ids) != direction_ids:
            raise HTTPException(status_code=400, detail="Invalid direction source reference")
        for row in rows:
            _assert_reference_revision(
                reference_claims.get(("direction", row.direction_key)),
                revision_values=(row.updated_at.isoformat() if row.updated_at else None,),
                detail="Direction source",
            )

    return context, reply_target


def _work_session_command_by_id(
    state: Dict[str, Any],
    command_id: str,
) -> Dict[str, Any] | None:
    work_session = state.get("work_session")
    commands = (
        work_session.get("commands")
        if isinstance(work_session, dict)
        else None
    )
    if not isinstance(commands, list):
        return None
    for command in commands:
        if isinstance(command, dict) and command.get("id") == command_id:
            return command
    return None


_SESSION_CONVERSATION_LIMIT = 20
_SESSION_OUTPUT_LIMIT = 8


def _session_conversation_predicate():
    return ProjectMessage.surface == MessageSurface.CONVERSATION


def _message_preview(
    message: ProjectMessage,
    *,
    context_package: Dict[str, Any] | None = None,
    selected_ref_descriptors: dict[tuple[str, str], Dict[str, Any]] | None = None,
    content_max_chars: int | None = 280,
) -> Dict[str, Any]:
    preview = message_preview_dict(message, content_max_chars=content_max_chars)
    preview["client_idempotency_key"] = preview.get("idempotency_key")
    context = preview.get("context")
    strategic_session = (
        context.get("strategic_session")
        if isinstance(context, dict)
        else None
    )
    if isinstance(strategic_session, dict):
        selected_refs = strategic_session.get("selected_refs")
        if isinstance(selected_refs, list):
            strategic_session["selected_ref_descriptors"] = [
                selected_ref_descriptor(
                    ref,
                    context_package,
                    selected_ref_descriptors,
                )
                for ref in selected_refs[:12]
                if isinstance(ref, dict)
            ]
    return preview


def _session_status_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
    local_runner = (
        runtime.get("local_runner") if isinstance(runtime.get("local_runner"), dict) else {}
    )
    strategic = state.get("strategic") if isinstance(state.get("strategic"), dict) else {}
    active_task = strategic.get("active_task")
    status = _compact_text(local_runner.get("status"), max_chars=40)
    location = _compact_text(local_runner.get("location"), max_chars=40)
    reason = _compact_text(local_runner.get("status_reason"), max_chars=80)
    if status is None:
        mode = local_runner.get("mode")
        active_task_runtime = local_runner.get("active_task_runtime")
        runtime_vm_id = (
            active_task_runtime.get("vm_instance_id")
            if isinstance(active_task_runtime, dict)
            else None
        )
        if active_task is not None and (
            mode == "local" or (mode == "cloud" and runtime_vm_id)
        ):
            status = "working"
        elif local_runner.get("mode") == "local_ready":
            status = "idle"
        else:
            status = "unavailable"
    if location is None:
        mode = local_runner.get("mode")
        active_task_runtime = local_runner.get("active_task_runtime")
        runtime_vm_id = (
            active_task_runtime.get("vm_instance_id")
            if isinstance(active_task_runtime, dict)
            else None
        )
        if mode in ("local", "local_ready", "pending_local"):
            location = "locally"
        elif mode == "cloud":
            location = "cloud" if active_task is not None and runtime_vm_id else "unavailable"
        else:
            location = "unavailable"
    return {"status": status, "location": location, "reason": reason}


def _session_output_from_message(
    message: ProjectMessage,
    *,
    context_package: Dict[str, Any] | None,
    selected_ref_descriptors: dict[tuple[str, str], Dict[str, Any]] | None,
) -> Dict[str, Any]:
    preview = _message_preview(
        message,
        context_package=context_package,
        selected_ref_descriptors=selected_ref_descriptors,
    )
    return {
        "kind": "message",
        "id": preview["id"],
        "message_type": preview["message_type"],
        "from_type": preview["from_type"],
        "from_task_id": preview.get("from_task_id"),
        "content": preview["content"],
        "status": preview["status"],
        "created_at": preview["created_at"],
        "context": preview.get("context") or {},
    }


def _session_output_from_cycle(cycle: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not isinstance(cycle, dict):
        return None
    content = (
        _compact_text(cycle.get("summary_text"), max_chars=300)
        or _compact_text(cycle.get("failure_reason"), max_chars=240)
    )
    if not content:
        return None
    created_at = (
        cycle.get("phase2_completed_at")
        or cycle.get("phase1_completed_at")
        or cycle.get("phase1_started_at")
    )
    return {
        "kind": "cycle",
        "id": cycle.get("id"),
        "message_type": "NOTE",
        "from_type": "strategic",
        "content": content,
        "status": cycle.get("outcome"),
        "created_at": created_at,
        "context": {
            "phase1_task_id": cycle.get("phase1_task_id"),
            "phase2_task_id": cycle.get("phase2_task_id"),
        },
    }


async def _recent_session_conversation(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    context_package: Dict[str, Any] | None,
    include_message_id: uuid.UUID | None = None,
    before_sequence: int | None = None,
    after_sequence: int | None = None,
    limit: int = _SESSION_CONVERSATION_LIMIT,
    search: str | None = None,
) -> Dict[str, Any]:
    base_conditions: list[Any] = [
        ProjectMessage.project_id == project_id,
        _session_conversation_predicate(),
    ]
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        base_conditions.append(ProjectMessage.content.ilike(f"%{escaped}%", escape="\\"))
    included_message: ProjectMessage | None = None
    if include_message_id is not None:
        included_message = (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.id == include_message_id,
                    *base_conditions,
                )
            )
        ).scalars().first()
        if included_message is None:
            raise HTTPException(status_code=404, detail="Conversation message not found")

    if included_message is not None and included_message.sequence is not None:
        before_count = limit // 2
        after_count = max(0, limit - before_count - 1)
        before_rows = (
            await session.execute(
                select(ProjectMessage)
                .where(*base_conditions, ProjectMessage.sequence < included_message.sequence)
                .order_by(ProjectMessage.sequence.desc())
                .limit(before_count)
            )
        ).scalars().all()
        after_rows = (
            await session.execute(
                select(ProjectMessage)
                .where(*base_conditions, ProjectMessage.sequence > included_message.sequence)
                .order_by(ProjectMessage.sequence.asc())
                .limit(after_count)
            )
        ).scalars().all()
        # Fill an uneven edge from the other side while preserving the limit.
        missing = limit - (len(before_rows) + 1 + len(after_rows))
        if missing and len(before_rows) < before_count:
            after_rows = (
                await session.execute(
                    select(ProjectMessage)
                    .where(*base_conditions, ProjectMessage.sequence > included_message.sequence)
                    .order_by(ProjectMessage.sequence.asc())
                    .limit(after_count + missing)
                )
            ).scalars().all()
        elif missing and len(after_rows) < after_count:
            before_rows = (
                await session.execute(
                    select(ProjectMessage)
                    .where(*base_conditions, ProjectMessage.sequence < included_message.sequence)
                    .order_by(ProjectMessage.sequence.desc())
                    .limit(before_count + missing)
                )
            ).scalars().all()
        visible_rows = [*reversed(before_rows), included_message, *after_rows]
        first_sequence = visible_rows[0].sequence
        has_more = bool(
            first_sequence is not None
            and await session.scalar(
                select(ProjectMessage.id)
                .where(*base_conditions, ProjectMessage.sequence < first_sequence)
                .limit(1)
            )
        )
    elif after_sequence is not None:
        rows = (
            await session.execute(
                select(ProjectMessage)
                .where(*base_conditions, ProjectMessage.sequence > after_sequence)
                .order_by(ProjectMessage.sequence.asc())
                .limit(limit + 1)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
    else:
        stmt = select(ProjectMessage).where(*base_conditions)
        if before_sequence is not None:
            stmt = stmt.where(ProjectMessage.sequence < before_sequence)
        rows = (
            await session.execute(
                stmt.order_by(
                    ProjectMessage.sequence.desc().nullslast(),
                    ProjectMessage.created_at.desc(),
                    ProjectMessage.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        visible_rows = list(reversed(rows[:limit]))

    selected_ref_descriptors = await resolve_selected_ref_descriptors(
        session,
        project_id,
        _collect_selected_refs_for_resolution(*visible_rows),
        limit=48,
    )
    return {
        "limit": limit,
        "has_more": has_more,
        "next_before_sequence": (
            visible_rows[0].sequence
            if has_more and visible_rows and visible_rows[0].sequence is not None
            else None
        ),
        "next_after_sequence": (
            visible_rows[-1].sequence
            if after_sequence is not None and visible_rows
            else None
        ),
        "order": "oldest_first",
        "items": [
            _message_preview(
                message,
                context_package=context_package,
                selected_ref_descriptors=selected_ref_descriptors,
                content_max_chars=None,
            )
            for message in visible_rows
        ],
    }


async def _recent_session_outputs(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    latest_cycle: Dict[str, Any] | None,
    context_package: Dict[str, Any] | None,
) -> list[Dict[str, Any]]:
    outputs: list[Dict[str, Any]] = []
    cycle_output = _session_output_from_cycle(latest_cycle)
    if cycle_output is not None:
        outputs.append(cycle_output)
    message_rows = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type.in_(["strategic", "system"]),
                or_(
                    ProjectMessage.context["event_type"].as_string().is_(None),
                    and_(
                        ProjectMessage.context["event_type"].as_string()
                        != _CHECK_IN_EVENT_TYPE,
                        ProjectMessage.context["event_type"].as_string()
                        != OPERATOR_COMMAND_EVENT_TYPE,
                    ),
                ),
            )
            .order_by(ProjectMessage.created_at.desc())
            .limit(_SESSION_OUTPUT_LIMIT)
        )
    ).scalars().all()
    selected_ref_descriptors = await resolve_selected_ref_descriptors(
        session,
        project_id,
        _collect_selected_refs_for_resolution(*message_rows),
        limit=48,
    )
    outputs.extend(
        _session_output_from_message(
            message,
            context_package=context_package,
            selected_ref_descriptors=selected_ref_descriptors,
        )
        for message in message_rows
    )
    outputs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return outputs[:_SESSION_OUTPUT_LIMIT]


async def _build_strategic_session_response(
    session: AsyncSession,
    project_id: uuid.UUID,
    state: Dict[str, Any],
    *,
    include_message_id: uuid.UUID | None = None,
) -> Dict[str, Any]:
    context_package = (
        state.get("context_package")
        if isinstance(state.get("context_package"), dict)
        else None
    )
    conversation = await _recent_session_conversation(
        session,
        project_id,
        context_package=context_package,
        include_message_id=include_message_id,
    )
    latest_cycle = (
        (state.get("strategic") or {}).get("latest_cycle")
        if isinstance(state.get("strategic"), dict)
        else None
    )
    recent_outputs = await _recent_session_outputs(
        session,
        project_id,
        latest_cycle=latest_cycle,
        context_package=context_package,
    )
    status = _session_status_from_state(state)
    runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
    local_runner = (
        runtime.get("local_runner") if isinstance(runtime.get("local_runner"), dict) else {}
    )
    work_session = dict(state.get("work_session") or {})
    work_session.update(
        {
            "status": status["status"],
            "location": status["location"],
            "status_reason": status["reason"],
            "runtime": local_runner,
            "conversation": conversation,
            "recent_outputs": recent_outputs,
        }
    )
    return {
        "project_id": str(project_id),
        "status": status["status"],
        "location": status["location"],
        "status_reason": status["reason"],
        "session": work_session,
        "workbench": state.get("workbench"),
        "strategic": state.get("strategic"),
        "resident_session": state.get("resident_session"),
        "runtime": runtime,
        "context_package": context_package,
        "conversation": work_session["conversation"],
        "recent_outputs": recent_outputs,
    }


async def _message_preview_for_session_state(
    session: AsyncSession,
    project_id: uuid.UUID,
    state_response: Dict[str, Any],
    message: ProjectMessage,
) -> Dict[str, Any]:
    context_package = (
        state_response.get("context_package")
        if isinstance(state_response.get("context_package"), dict)
        else None
    )
    selected_ref_descriptors = await resolve_selected_ref_descriptors(
        session,
        project_id,
        _collect_selected_refs_for_resolution(message),
        limit=48,
    )
    return _message_preview(
        message,
        context_package=context_package,
        selected_ref_descriptors=selected_ref_descriptors,
        content_max_chars=None,
    )


def _selected_ref_descriptor_key(
    descriptor: Any,
) -> tuple[str, str] | None:
    if not isinstance(descriptor, dict):
        return None
    kind = _compact_text(descriptor.get("kind"), max_chars=40)
    ref_id = _compact_text(descriptor.get("id"), max_chars=160)
    if not kind or not ref_id:
        return None
    return (kind.lower(), ref_id)


def _descriptor_has_resolved_context(descriptor: Any) -> bool:
    return isinstance(descriptor, dict) and any(
        key in descriptor for key in ("summary", "status", "target")
    )


def _merge_stored_and_preview_ref_descriptors(
    stored_descriptors: Any,
    preview_descriptors: list[Any],
) -> list[Any]:
    if not isinstance(stored_descriptors, list) or not stored_descriptors:
        return preview_descriptors

    preview_by_key: dict[tuple[str, str], Any] = {}
    for descriptor in preview_descriptors:
        key = _selected_ref_descriptor_key(descriptor)
        if key is not None and key not in preview_by_key:
            preview_by_key[key] = descriptor

    merged: list[Any] = []
    stored_keys: set[tuple[str, str]] = set()
    for descriptor in stored_descriptors:
        key = _selected_ref_descriptor_key(descriptor)
        if key is None:
            merged.append(descriptor)
            continue
        stored_keys.add(key)
        preview = preview_by_key.get(key)
        if _descriptor_has_resolved_context(preview):
            merged.append(preview)
        else:
            merged.append(descriptor)

    for descriptor in preview_descriptors:
        key = _selected_ref_descriptor_key(descriptor)
        if key is None or key in stored_keys:
            continue
        if _descriptor_has_resolved_context(descriptor):
            merged.append(descriptor)

    return merged


async def _persist_message_preview_strategic_session_descriptors(
    session: AsyncSession,
    message: ProjectMessage,
    message_preview: Dict[str, Any],
) -> None:
    preview_context = (
        message_preview.get("context")
        if isinstance(message_preview.get("context"), dict)
        else {}
    )
    preview_snapshot = (
        preview_context.get("strategic_session")
        if isinstance(preview_context, dict)
        else None
    )
    if not isinstance(preview_snapshot, dict):
        return
    preview_descriptors = preview_snapshot.get("selected_ref_descriptors")
    if not isinstance(preview_descriptors, list) or not preview_descriptors:
        return

    context = message.context if isinstance(message.context, dict) else {}
    stored_snapshot = context.get("strategic_session")
    if not isinstance(stored_snapshot, dict):
        return
    stored_descriptors = stored_snapshot.get("selected_ref_descriptors")
    next_descriptors = _merge_stored_and_preview_ref_descriptors(
        stored_descriptors,
        preview_descriptors,
    )

    updated_snapshot = sanitize_strategic_session_snapshot(
        {
            **stored_snapshot,
            "selected_ref_descriptors": next_descriptors,
        }
    )
    if updated_snapshot == stored_snapshot:
        return
    message.context = {
        **context,
        "strategic_session": updated_snapshot,
    }
    flag_modified(message, "context")
    session.add(message)
    await session.commit()
    await session.refresh(message)


def _compute_strategic_state_revision(
    project: Project,
    *,
    active_cycle_summary: Dict[str, Any] | None,
    active_task: Task | None,
    latest_cycle_summary: Dict[str, Any] | None,
    running_update_summary: Dict[str, Any],
    problem_model_summary: Dict[str, Any],
    directions_summary: Dict[str, Any],
    message_counts: Dict[str, int],
    task_counts: Dict[str, int],
    attention: Dict[str, Any] | None,
    fingerprints: Dict[str, Any] | None = None,
) -> str:
    """Compute a lightweight revision token for the strategic-state payload.

    Covers both the context-package inputs and — via the optional
    ``fingerprints`` map of cheap Phase-1 scalar/count/max-timestamp probes —
    the segments the narrow token historically did NOT reach (runtime runners /
    compute jobs / wakes, workbench, event feed, command jobs, integrity / open
    version). Called AFTER the cheap inputs are resolved and BEFORE the
    expensive Phase-2 assembly, so a matching client token lets the whole
    assembly be skipped with an HTTP 304.

    ``fingerprints`` defaults to ``None`` so the historical (context-package
    only) signature and its callers keep working; when provided it is folded
    into the hashed seed under a stable key, making the token change iff a
    fingerprinted segment changed.
    """
    seed = json.dumps(
        {
            "project_id": str(project.id),
            "project_name": project.name,
            "vision": project.vision or {},
            "problem_model": project.problem_model or {},
            "directions": project.directions or [],
            "design_doc": bool(project.design_doc),
            "has_running_update": bool(project.running_update),
            "running_update_acknowledged_at": (
                str(project.running_update_acknowledged_at)
                if project.running_update_acknowledged_at
                else None
            ),
            "active_cycle": active_cycle_summary or {},
            "active_task_id": str(active_task.id) if active_task else None,
            "active_task_updated_at": (
                str(active_task.updated_at)
                if active_task and active_task.updated_at
                else None
            ),
            "latest_cycle": latest_cycle_summary or {},
            "running_update_summary": running_update_summary,
            "problem_model_summary": problem_model_summary,
            "directions_summary": directions_summary,
            "message_counts": message_counts,
            "task_counts": task_counts,
            "attention_level": (attention or {}).get("level"),
            "attention_counts": (attention or {}).get("counts", {}),
            "fingerprints": fingerprints or {},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


async def _compute_strategic_state_fingerprints(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: str,
) -> Dict[str, Any]:
    """Cheap Phase-1 fingerprint probes for the strategic-state segments the
    narrow context-package revision never covered.

    Each probe is a single indexed scalar / count / max-timestamp — collectively
    O(1)-ish — chosen so its value changes iff the corresponding Phase-2 segment
    would render differently:

    * ``workbench_updated_at`` — per-user workbench prefs row (MessagesNoisePrefs)
    * ``runner_count`` / ``runner_max_seen`` / ``live_runner_count`` — enrolled
      runners, latest heartbeat, and the count currently INSIDE the liveness
      window (runtime local_runner + resident-session picker + availability).
      ``live_runner_count`` is time-derived (mirrors ``_runner_health``'s
      ``is_live``) so a runner heartbeat going stale — which does NOT move
      ``runner_max_seen`` or ``runner_count`` — still flips the revision the
      instant it crosses ``_RUNNER_LIVENESS_SECONDS``.
    * ``active_compute_count`` / ``compute_max_updated`` — in-flight compute jobs
    * ``active_wake_count`` — parked task wakes still pending/fired
    * ``message_max_created_at`` / ``message_max_resolved_at`` — message previews,
      event feed, and command-job feed (all ProjectMessage-derived)
    * ``task_max_updated_at`` — event feed task rows + task focus
    * ``alert_max_at`` — event feed alert rows
    * ``open_version_updated_at`` — sha-freshness open version
    * ``direction_count`` / ``direction_max_updated_at`` — Direction rows whose
      ``criterion_keys`` / ``status`` feed model-integrity + criterion-links (these
      live on the Direction table, NOT the project.directions blob the narrow
      token folds in, so an integrity edit is invisible without this probe).
    * ``completion_debt_count`` / ``completion_debt_max_updated`` — durable
      completion-debt attention sources, which are not ProjectMessage rows.
    * ``attention_next_time_boundary`` — the next known clock transition for
      attention's ten-minute dependency queue, twenty-four-hour debt, or
      six-hour experiment windows.  This remains stable between transitions,
      unlike hashing ``now`` on every poll.

    A DB error here would silently over-suppress (a stale probe → false 304), so
    the caller runs this on the always-executed Phase-1 path where any failure
    surfaces as a normal 500 rather than a wrong-but-200; we deliberately do NOT
    swallow exceptions.
    """
    workbench_updated_at = await session.scalar(
        select(func.max(MessagesNoisePrefs.updated_at)).where(
            MessagesNoisePrefs.project_id == project_id,
            MessagesNoisePrefs.user_id == user_id,
        )
    )
    attention_last_seen_updated_at = await session.scalar(
        select(MessagesLastSeen.updated_at).where(
            MessagesLastSeen.project_id == project_id,
            MessagesLastSeen.user_id == user_id,
        )
    )
    runner_row = (
        await session.execute(
            select(func.count(), func.max(LocalRunner.last_seen_at)).where(
                runner_allowed_for_project(project_id),
                LocalRunner.status != "deleted",
            )
        )
    ).one()
    # Time-derived liveness: count runners _runner_health would mark is_live=True
    # right now (status=='active' AND heartbeat within the liveness window). When
    # a live runner stops heartbeating, last_seen_at is frozen so runner_max_seen
    # cannot catch the transition — this count drops from N to N-1 the moment the
    # frozen heartbeat ages past the cutoff, flipping the revision.
    _liveness_cutoff = _utcnow() - _dt.timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    live_runner_count = await session.scalar(
        select(func.count()).where(
            runner_allowed_for_project(project_id),
            LocalRunner.status == "active",
            LocalRunner.last_seen_at.is_not(None),
            LocalRunner.last_seen_at >= _liveness_cutoff,
        )
    )
    direction_row = (
        await session.execute(
            select(func.count(), func.max(Direction.updated_at)).where(
                Direction.project_id == project_id,
            )
        )
    ).one()
    compute_row = (
        await session.execute(
            select(func.count(), func.max(ComputeJob.updated_at)).where(
                ComputeJob.project_id == project_id,
                ComputeJob.status.in_(_FINGERPRINT_ACTIVE_COMPUTE_JOB_STATUSES),
            )
        )
    ).one()
    active_wake_count = await session.scalar(
        select(func.count()).where(
            TaskWakeRegistration.project_id == project_id,
            TaskWakeRegistration.delivery_state.in_(
                _FINGERPRINT_ACTIVE_WAKE_STATES
            ),
        )
    )
    message_row = (
        await session.execute(
            select(
                func.max(ProjectMessage.created_at),
                func.max(ProjectMessage.resolved_at),
            ).where(ProjectMessage.project_id == project_id)
        )
    ).one()
    task_max_updated_at = await session.scalar(
        select(func.max(Task.updated_at)).where(Task.project_id == project_id)
    )
    alert_max_at = await session.scalar(
        select(
            func.max(func.coalesce(Alert.dismissed_at, Alert.created_at))
        ).where(Alert.project_id == project_id)
    )
    open_version_updated_at = await session.scalar(
        select(func.max(ReleaseVersion.updated_at)).where(
            ReleaseVersion.project_id == project_id,
            ReleaseVersion.status == "open",
        )
    )
    completion_debt_row = (
        await session.execute(
            select(func.count(), func.max(CompletionDebtEntry.updated_at)).where(
                or_(
                    CompletionDebtEntry.project_id == project_id,
                    CompletionDebtEntry.project_id.is_(None),
                )
            )
        )
    ).one()

    # Attention has several clock-derived transitions.  Query only the nearest
    # candidate timestamp for each window, then fingerprint that *boundary*;
    # this keeps the early 304 fast path intact between actual transitions.
    now = _utcnow()
    next_dependency_created_at = await session.scalar(
        select(func.min(Task.created_at)).where(
            Task.project_id == project_id,
            Task.archived.is_(False),
            Task.status == TaskStatus.QUEUED,
            Task.created_at > now - _dt.timedelta(minutes=10),
        )
    )
    next_debt_created_at = await session.scalar(
        select(func.min(CompletionDebtEntry.created_at)).where(
            or_(
                CompletionDebtEntry.project_id == project_id,
                CompletionDebtEntry.project_id.is_(None),
            ),
            CompletionDebtEntry.status == "open",
            CompletionDebtEntry.created_at > now - _dt.timedelta(hours=24),
        )
    )
    next_experiment_completed_at = await session.scalar(
        select(func.min(Task.completed_at)).where(
            Task.project_id == project_id,
            Task.archived.is_(False),
            Task.status.in_((TaskStatus.COMPLETED, TaskStatus.FAILED)),
            Task.completed_at.is_not(None),
            Task.completed_at > now - _dt.timedelta(hours=6),
        )
    )
    next_attestation_recorded_at = await session.scalar(
        select(func.min(CriterionEvidence.recorded_at)).where(
            CriterionEvidence.project_id == project_id,
            CriterionEvidence.source_kind == "operator_attestation",
            CriterionEvidence.recorded_at > now - _dt.timedelta(days=7),
        )
    )
    boundaries = [
        candidate
        for candidate in (
            (
                next_dependency_created_at + _dt.timedelta(minutes=10)
                if next_dependency_created_at
                else None
            ),
            (
                next_debt_created_at + _dt.timedelta(hours=24)
                if next_debt_created_at
                else None
            ),
            (
                next_experiment_completed_at + _dt.timedelta(hours=6)
                if next_experiment_completed_at
                else None
            ),
            # An attestation first becomes a warning two days before its
            # seven-day expiry, then changes again when it actually expires.
            (
                next_attestation_recorded_at + _dt.timedelta(days=5)
                if next_attestation_recorded_at
                else None
            ),
            (
                next_attestation_recorded_at + _dt.timedelta(days=7)
                if next_attestation_recorded_at
                else None
            ),
        )
        if candidate is not None and candidate > now
    ]
    attention_next_time_boundary = min(boundaries) if boundaries else None
    return {
        "workbench_updated_at": str(workbench_updated_at)
        if workbench_updated_at
        else None,
        # Marking messages seen changes unread attention counts without changing
        # any message or task row, so this per-user source must flip the guard.
        "attention_last_seen_updated_at": str(attention_last_seen_updated_at)
        if attention_last_seen_updated_at
        else None,
        "runner_count": int(runner_row[0] or 0),
        "runner_max_seen": str(runner_row[1]) if runner_row[1] else None,
        "live_runner_count": int(live_runner_count or 0),
        "direction_count": int(direction_row[0] or 0),
        "direction_max_updated_at": str(direction_row[1]) if direction_row[1] else None,
        "active_compute_count": int(compute_row[0] or 0),
        "compute_max_updated": str(compute_row[1]) if compute_row[1] else None,
        "active_wake_count": int(active_wake_count or 0),
        "message_max_created_at": str(message_row[0]) if message_row[0] else None,
        "message_max_resolved_at": str(message_row[1]) if message_row[1] else None,
        "task_max_updated_at": str(task_max_updated_at)
        if task_max_updated_at
        else None,
        "alert_max_at": str(alert_max_at) if alert_max_at else None,
        "open_version_updated_at": str(open_version_updated_at)
        if open_version_updated_at
        else None,
        "completion_debt_count": int(completion_debt_row[0] or 0),
        "completion_debt_max_updated": str(completion_debt_row[1])
        if completion_debt_row[1]
        else None,
        "attention_next_time_boundary": str(attention_next_time_boundary)
        if attention_next_time_boundary
        else None,
    }


# Pure wall-clock stamps / derived durations that advance on every poll without
# a real state change. They are excluded from the full-body ETag so an unchanged
# poll yields a matching token (and thus a 304). Everything NOT listed here is
# covered by the ETag by default, so broadening the response never risks a false
# 304 — at worst an un-normalized noise field makes the ETag over-sensitive
# (a redundant 200), never under-sensitive.
_STRATEGIC_STATE_ETAG_VOLATILE_KEYS = frozenset(
    {
        # sha_freshness stamps: datetime.now() on every call.
        "checked_at",
        "generated_at",
        # Runner health: a derived now() - last_seen_at duration. The underlying
        # last_seen_at / is_live / device_status remain in the hash and cover the
        # meaningful liveness transitions.
        "seconds_since_last_seen",
        # since_last_check_in window end is always "now"; "since" (the real last
        # check-in time) stays in the hash.
        "until",
    }
)


def _strip_volatile_for_etag(obj: Any) -> Any:
    """Recursively drop pure-clock noise keys so the ETag is stable across
    unchanged polls. See ``_STRATEGIC_STATE_ETAG_VOLATILE_KEYS``."""
    if isinstance(obj, dict):
        return {
            k: _strip_volatile_for_etag(v)
            for k, v in obj.items()
            if k not in _STRATEGIC_STATE_ETAG_VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [_strip_volatile_for_etag(v) for v in obj]
    return obj


def _compute_strategic_state_etag(result: Dict[str, Any]) -> str:
    """Full-body revision token used ONLY for the HTTP 304 transmission
    short-circuit.

    Hashes the ENTIRE assembled response body — so ANY change to a volatile
    field the narrow ``state_revision`` does not cover (runtime_summary,
    resident_runner_options, workbench_state, work_session, model_integrity,
    sha_freshness verdict, costs, counts, …) flips the token — with two
    deliberate exclusions:

    * ``context_package`` embeds per-request signed download URLs that change
      every poll; its meaningful inputs are already reflected in the top-level
      strategic / problem_model / directions / message+task count / attention
      fields (and gated by the narrow ``state_revision`` build-skip), so
      dropping the raw block keeps the token stable without hiding real changes.
    * pure wall-clock stamps / derived durations (see the volatile-keys set).

    This is intentionally SEPARATE from ``_compute_strategic_state_revision``
    (which stays the narrow context-package build-skip token) — do not conflate
    them.
    """
    projected = {
        k: v
        for k, v in result.items()
        if k not in ("context_package", "state_revision")
    }
    projected["work_session"] = _drop_context_derived_for_etag(
        projected.get("work_session")
    )
    seed = json.dumps(
        _strip_volatile_for_etag(projected), sort_keys=True, default=str
    )
    return hashlib.sha256(seed.encode()).hexdigest()


def _drop_context_derived_for_etag(work_session: Any) -> Any:
    """Drop the parts of ``work_session`` that MIRROR the context package.

    ``context_package`` is deliberately excluded from the ETag (its signed URLs
    change every poll), but ``work_session`` re-projects it: ``context_package_id``,
    ``context_sources``, and the ``commands`` / ``curated_metrics`` entries tagged
    ``provenance.surface == "context"``.  On a build-SKIPPED poll those collapse
    (``context.items`` reports 0, a command disappears), so the ETag flipped on
    every skip — a client's held token could never 304 and the whole body was
    re-transmitted every time, defeating the short-circuit the ETag exists for.

    Excluding the mirror is consistent with excluding the original: the context
    package's meaningful INPUTS are already covered by the top-level strategic /
    problem_model / directions / count / attention fields, and the narrow
    ``state_revision`` gates the build-skip itself, so nothing real is hidden.

    NOTE (separate bug, deliberately not masked here): the build-skip DEGRADES
    ``work_session`` rather than merely omitting the raw block — a skipped poll
    reports ``context.items = 0`` when the client's cached package holds 8. That
    is a response-fidelity bug in the skip path, not an ETag bug; this function
    only stops it from corrupting the ETag.
    """
    if not isinstance(work_session, dict):
        return work_session
    out = {
        k: v
        for k, v in work_session.items()
        if k not in ("context_package_id", "context_sources")
    }
    for list_key in ("commands", "curated_metrics"):
        items = out.get(list_key)
        if isinstance(items, list):
            out[list_key] = [it for it in items if not _is_context_derived_entry(it)]
    return out


def _is_context_derived_entry(entry: Any) -> bool:
    """True when a work_session command/metric is a projection of the context package.

    Two shapes carry that tag: an explicit ``provenance.surface == "context"``
    (the curated metrics), and a ``context.``-namespaced id (the commands — e.g.
    ``context.canvas.open``, which is only offered when a package was built).
    """
    if not isinstance(entry, dict):
        return False
    provenance = entry.get("provenance")
    if isinstance(provenance, dict) and provenance.get("surface") == "context":
        return True
    entry_id = entry.get("id")
    return isinstance(entry_id, str) and entry_id.startswith("context.")


def _quote_etag(token: str) -> str:
    """Format a raw hex token as a strong HTTP ETag header value."""
    return f'"{token}"'


def _if_none_match_hits(if_none_match: object | None, etag: str) -> bool:
    """Return True when a client ``If-None-Match`` header matches the current
    full-body ``etag``.

    Tolerates the standard header shapes a proxy/browser may produce: a
    comma-separated list, weak validators (``W/"…"``), surrounding quotes, and
    the ``*`` wildcard.

    Only a real ``str`` is a header VALUE.  ``get_project_strategic_state`` is a
    FastAPI endpoint whose ``if_none_match`` default is a ``Header(...)`` marker
    object, and that default is resolved to a string ONLY by dependency injection
    on a real HTTP request.  The six INTERNAL direct callers in this module call it
    as a plain function and omit the argument — so they received the ``Header``
    marker itself, which is truthy, fell through to ``.split(",")`` and raised
    ``AttributeError: 'Header' object has no attribute 'split'``, 500-ing
    ``POST /strategic-session/messages`` (and every other internal caller).

    Treat any non-``str`` as ABSENT: an internal caller has no request headers, so
    "no If-None-Match → always return the full body" is exactly the documented
    intent at the call sites.  Guarding here (rather than at each of the six call
    sites) also fail-safes any future direct caller.
    """
    if not isinstance(if_none_match, str) or not if_none_match:
        return False
    for raw in if_none_match.split(","):
        candidate = raw.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if len(candidate) >= 2 and candidate[0] == '"' and candidate[-1] == '"':
            candidate = candidate[1:-1]
        if candidate == etag:
            return True
    return False


@router.get("/projects/{project_id}/strategic-state")
@limiter.limit("30/minute")
async def get_project_strategic_state(
    request: Request,
    project_id: uuid.UUID,
    response: Response,
    state_revision: str | None = Query(None),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a compact, persistent Strategic workbench read model.

    This intentionally projects existing durable state instead of creating a new
    chat/session subsystem. The chat surface can reload from this endpoint after
    a browser refresh, while deeper editing still lives in the Project workspace.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Sequential session.execute() calls: AsyncSession does not support
    # concurrent coroutine-based calls (asyncpg raises InterfaceError when
    # a second query is issued before the first completes).
    # Both the most-recent active cycle and the most-recent non-active
    # ("latest") cycle in one round-trip via DISTINCT ON the is-active flag:
    # Postgres keeps one row per distinct flag value, and the ORDER BY (flag
    # leading, then phase1_started_at DESC) makes that the newest of each group.
    # Equivalent to the two prior limit-1 queries but halves the round-trips.
    _is_active_cycle = StrategicCycle.outcome.in_(list(strategic_cycle.ACTIVE_OUTCOMES))
    _cycle_res = await session.execute(
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project.id)
        .order_by(
            _is_active_cycle.desc(),
            StrategicCycle.phase1_started_at.desc(),
        )
        .distinct(_is_active_cycle)
    )
    _task_status_rows_res = await session.execute(
        select(Task.status, func.count())
        .where(Task.project_id == project.id, Task.archived.is_(False))
        .group_by(Task.status)
    )
    _message_rows_res = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
        )
        .order_by(ProjectMessage.created_at.desc())
        .limit(12)
    )
    _message_count_rows_res = await session.execute(
        select(ProjectMessage.status, func.count())
        .where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
        )
        .group_by(ProjectMessage.status)
    )
    task_focus = await _build_task_focus(session, project.id)
    _open_version_res = await session.execute(
        select(ReleaseVersion)
        .where(
            ReleaseVersion.project_id == project.id,
            ReleaseVersion.status == "open",
        )
        .order_by(ReleaseVersion.opened_at.desc())
        .limit(1)
    )
    _cycle_rows = _cycle_res.scalars().all()
    active_cycle = next(
        (c for c in _cycle_rows if c.outcome in strategic_cycle.ACTIVE_OUTCOMES),
        None,
    )
    latest_cycle = next(
        (c for c in _cycle_rows if c.outcome not in strategic_cycle.ACTIVE_OUTCOMES),
        None,
    )
    task_status_rows = _task_status_rows_res.all()
    message_rows = _message_rows_res.scalars().all()
    message_count_rows = _message_count_rows_res.all()
    open_version = _open_version_res.scalars().first()

    active_task = None
    active_phase = None
    if active_cycle is not None:
        task_id = None
        if active_cycle.outcome == strategic_cycle.OUTCOME_PHASE2_RUNNING:
            task_id = active_cycle.phase2_task_id
            active_phase = "strategic_review"
        elif active_cycle.phase1_task_id:
            task_id = active_cycle.phase1_task_id
            active_phase = "design_doc_update"
        if task_id is not None:
            candidate_task = await session.get(Task, task_id)
            if _is_live_strategic_task(candidate_task):
                active_task = candidate_task
            else:
                active_phase = None

    task_counts = {
        "queued": 0,
        "running": 0,
        "evaluating": 0,
        "blocked": 0,
        "parked": 0,
        "failed": 0,
        "completed": 0,
        "cancelled": 0,
        "total": 0,
    }
    for status, count in task_status_rows:
        n = int(count or 0)
        task_counts["total"] += n
        if status == TaskStatus.QUEUED:
            task_counts["queued"] += n
        elif status in (TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING):
            task_counts["running"] += n
        elif status == TaskStatus.EVALUATING:
            task_counts["evaluating"] += n
        elif status == TaskStatus.BLOCKED:
            task_counts["blocked"] += n
        elif status == TaskStatus.PARKED:
            task_counts["parked"] += n
        elif status == TaskStatus.FAILED:
            task_counts["failed"] += n
        elif status == TaskStatus.COMPLETED:
            task_counts["completed"] += n
        elif status == TaskStatus.CANCELLED:
            task_counts["cancelled"] += n

    # Aggregate estimated_cost_usd in SQL to avoid materializing every non-archived
    # task row. estimated_cost_usd is set at completion (terminal success/failure)
    # to the full VM cost (banked retries + current-attempt interval), and is also
    # updated by bank_task_current_attempt on retry/requeue paths. This correctly
    # captures single-attempt completed tasks whose cost was never banked into
    # vm_runtime_cost_banked_db. Active tasks with no prior attempts have
    # estimated_cost_usd=NULL and contribute 0, matching the original behavior
    # (current-attempt cost requires Python pricing logic and vm_stopped_at).
    # Result is cached for _COST_CACHE_TTL_S to avoid a full-table scan on
    # every 30-second poll tick.
    _cost_key = str(project.id)
    _cost_now = time.monotonic()
    _cost_cached = _COST_CACHE.get(_cost_key)
    if _cost_cached is not None and _cost_cached[1] > _cost_now:
        cost_total = _cost_cached[0]
    else:
        cost_total = (
            await session.execute(
                select(
                    func.coalesce(func.sum(Task.estimated_cost_usd), 0.0)
                ).where(
                    Task.project_id == project.id,
                    Task.archived.is_(False),
                )
            )
        ).scalar() or 0.0
        _COST_CACHE[_cost_key] = (float(cost_total), _cost_now + _COST_CACHE_TTL_S)
    message_counts = {"open": 0, "linked": 0}
    for status, count in message_count_rows:
        if status in message_counts:
            message_counts[status] = int(count or 0)

    active_cycle_summary = _cycle_summary(active_cycle, active_task=active_task)
    latest_cycle_summary = _cycle_summary(latest_cycle)
    running_update_summary = _running_update_summary(project)
    problem_model_summary = _problem_model_summary(project.problem_model or {})
    directions_summary = _direction_summary(project.directions or [])

    # Broad-revision fingerprints: a handful of single-row indexed scalar/count/
    # max-timestamp probes standing in for each Phase-2 segment the narrow token
    # never covered. Folded into the revision hash below so the token is stable
    # iff the visible response is stable — letting a matching poll return 304
    # BEFORE the expensive Phase-2 assembly runs. AsyncSession is single-threaded,
    # so these run sequentially; each is O(1)/small GROUP BY over indexed columns.
    async with async_timed(request, "fingerprints"):
        fingerprints = await _compute_strategic_state_fingerprints(
            session, project.id, current_user["user_id"]
        )
    # sha_freshness lives in an out-of-band memory cache the DB probes above can
    # never see; fold its cache-state so the cold→warm verdict transition flips
    # the revision instead of being masked by an early 304 (see helper docstring).
    fingerprints["currency"] = _currency_cache_fingerprint(project, open_version)

    # Broad state-revision skip: a matching client token proves EVERY visible
    # segment (context package + runtime + workbench + events + jobs + integrity)
    # is unchanged, so the whole Phase-2 assembly can be skipped with an HTTP 304.
    _revision = _compute_strategic_state_revision(
        project,
        active_cycle_summary=active_cycle_summary,
        active_task=active_task,
        latest_cycle_summary=latest_cycle_summary,
        running_update_summary=running_update_summary,
        problem_model_summary=problem_model_summary,
        directions_summary=directions_summary,
        message_counts=message_counts,
        task_counts=task_counts,
        attention=None,
        fingerprints=fingerprints,
    )
    if state_revision is not None and state_revision == _revision:
        # Assembly-skip: return 304 Not Modified with an empty body BEFORE
        # running any Phase-2 builder. Internal direct callers never pass
        # state_revision (it defaults to None), so this path is reachable only
        # for a real HTTP GET carrying ?state_revision= and always yields the
        # full dict for internal callers.
        logger.info(
            "[STRATEGIC_STATE] 304 not modified for project %s "
            "(broad state_revision unchanged; Phase-2 assembly skipped)",
            project.id,
        )
        # Empty body, no full-body ETag (never assembled). The client keys this
        # path on ?state_revision= and keeps its held ETag untouched on a 304.
        return Response(status_code=304)

    # Attention uses bounded previews but still performs several queries.  The
    # revision fingerprint covers its source rows, so only construct it when a
    # changed response actually needs to be assembled.
    attention = await build_operator_attention(session, project, current_user["user_id"])

    # Revision miss (or first-load with no token): build the full context
    # package. With the broad guard above, reaching here means a visible segment
    # changed, so the package's inputs are (re)assembled unconditionally.
    context_evidence = await load_strategic_context_evidence(session, project.id)
    context_package = build_strategic_context_package_summary(
        project,
        active_cycle=active_cycle_summary,
        active_task=active_task,
        latest_cycle=latest_cycle,
        running_update=running_update_summary,
        problem_model=problem_model_summary,
        directions=directions_summary,
        message_counts=message_counts,
        task_counts=task_counts,
        attention=attention,
        evidence=context_evidence,
    )
    runtime_summary = await build_strategic_runtime_summary(
        session,
        project.id,
        active_task=active_task,
        project_vision=project.vision or {},
        operator_user_id=current_user["user_id"],
    )
    # Multi-device resident-session picker: every enrolled runner with its
    # online / agent-ready / eligible flags (for the auto-pick + override
    # dropdown) and, via ``_resident_session_availability``, the block-on-offline
    # verdict for a session pinned to a now-offline device.
    async with async_timed(request, "runners"):
        resident_runner_options = await build_resident_runner_options(session, project.id)
    async with async_timed(request, "workbench"):
        workbench_state = await get_strategic_workbench_state(
            session,
            current_user["user_id"],
            project.id,
        )
    async with async_timed(request, "refs"):
        resolved_selected_ref_descriptors = await resolve_selected_ref_descriptors(
            session,
            project.id,
            _collect_selected_refs_for_resolution(
                workbench_state.get("selected_refs"),
                *message_rows,
            ),
            limit=48,
        )
    work_session = _build_work_session_summary(
        project,
        workbench_state=workbench_state,
        context_package=context_package,
        selected_ref_descriptors=resolved_selected_ref_descriptors,
        attention=attention,
        runtime_summary=runtime_summary,
        message_counts=message_counts,
        task_counts=task_counts,
    )
    session_status = _session_status_from_state(
        {
            "strategic": {"active_task": _task_summary(active_task)},
            "runtime": runtime_summary,
        }
    )
    work_session = {
        **work_session,
        "status": session_status["status"],
        "location": session_status["location"],
        "status_reason": session_status["reason"],
    }
    work_session = {
        **work_session,
        **await _build_work_session_check_in_projection(
            session,
            project.id,
            current_user["user_id"],
        ),
    }
    work_session = {
        **work_session,
        "events": await build_workbench_event_feed(
            session,
            project.id,
            runtime_summary=runtime_summary,
        ),
        "jobs": await build_work_session_command_jobs(
            session,
            project.id,
            user_id=current_user["user_id"],
        ),
    }

    # SHA-aware freshness: resolve candidate vs integration HEAD, best-effort.
    # A GitHub API failure must not break the endpoint — verdict degrades to unknown.
    sha_freshness: dict | None = None
    from app.strategic_spawning import (
        _build_payload_staleness_block,
        _build_unknown_freshness_block,
        get_project_integration_branch,
    )

    if open_version is not None:
        try:
            _last_qual = open_version.last_qualification or {}
            _integration_branch = get_project_integration_branch(project)
            _candidate_sha = _last_qual.get("candidate_sha") or ""
            _cache_key = (str(project.id), _integration_branch, _candidate_sha)
            _now = time.monotonic()
            _cached = _CURRENCY_CACHE.get(_cache_key)
            if _cached is not None and _cached[1] > _now:
                # Warm cache: serve last resolved verdict without waiting on GitHub.
                sha_freshness = {
                    **_build_payload_staleness_block(_cached[0]),
                    "integration_branch": _integration_branch,
                }
            else:
                # Cold cache: return unknown immediately and resolve off the request path.
                # The background task writes to _CURRENCY_CACHE for the next request.
                _task = asyncio.create_task(
                    _currency_resolve_and_cache(
                        project.id, _last_qual, _integration_branch, _cache_key
                    )
                )
                _BG_TASKS.add(_task)
                _task.add_done_callback(_BG_TASKS.discard)
                sha_freshness = {
                    **_build_unknown_freshness_block(),
                    "integration_branch": _integration_branch,
                }
        except Exception:
            logger.exception(
                "[STRATEGIC_STATE] sha_freshness build failed for project %s",
                project.id,
            )
            sha_freshness = _build_unknown_freshness_block()
    else:
        sha_freshness = _build_unknown_freshness_block()

    # Criterion-mapping integrity: Direction.criterion_keys live on the
    # Direction rows (not the directions blob), so fetch them and the open
    # version's key sets to classify inactive/dangling/wrong-version links.
    # Best-effort — a DB hiccup here must not sink the whole endpoint, so it
    # degrades to gap-only integrity.
    criterion_links: list[dict] | None = None
    active_crit_keys = None
    inactive_crit_keys = None
    if open_version is not None:
        try:
            from app.direction_referential_guard import (
                load_version_criterion_key_sets,
            )

            active_crit_keys, inactive_crit_keys = (
                await load_version_criterion_key_sets(
                    session, project.id, open_version.id
                )
            )
            crit_rows = (
                (
                    await session.execute(
                        select(Direction).where(
                            Direction.project_id == project.id,
                            Direction.status == "active",
                        )
                    )
                )
                .scalars()
                .all()
            )
            criterion_links = [
                {
                    "id": row.direction_key,
                    "text": row.text or "",
                    "status": row.status,
                    "criterion_keys": list(row.criterion_keys or []),
                }
                for row in crit_rows
                if row.criterion_keys
            ]
        except Exception:
            logger.exception(
                "[STRATEGIC_STATE] criterion-link integrity resolve failed for project %s",
                project.id,
            )
            criterion_links = None
            active_crit_keys = None
            inactive_crit_keys = None

    model_integrity = compute_model_integrity_conflicts(
        project.problem_model,
        project.directions,
        criterion_links=criterion_links,
        active_criterion_keys=active_crit_keys,
        inactive_criterion_keys=inactive_crit_keys,
    )
    integrity_trend = await _integrity_conflict_trend(session, project.id)

    result: Dict[str, Any] = {
        "project_id": str(project.id),
        "strategic": {
            "active_cycle": active_cycle_summary,
            "active_task": _task_summary(active_task),
            "active_phase": active_phase,
            "latest_cycle": latest_cycle_summary,
            "refresh": _strategic_refresh_summary(project.vision or {}),
        },
        "resident_session": _resident_session_summary(project),
        "resident_session_available": _resident_session_available(),
        "resident_session_availability": _resident_session_availability(
            project,
            eligible_live_runner_count=(
                int(
                    (runtime_summary.get("local_runner") or {}).get(
                        "eligible_live_count"
                    )
                    or 0
                )
                if isinstance(runtime_summary, dict)
                and isinstance(runtime_summary.get("local_runner"), dict)
                else None
            ),
            runner_options=resident_runner_options,
        ),
        # Multi-device picker options: every enrolled runner with online /
        # agent-ready / eligible flags so the UI can render the auto-pick +
        # override dropdown. The currently pinned device rides on
        # ``resident_session.runner_id``.
        "resident_session_runners": resident_runner_options,
        "running_update": running_update_summary,
        "problem_model": problem_model_summary,
        "directions": directions_summary,
        "messages": {
            "counts": message_counts,
            "items": [
                _message_preview(
                    message,
                    context_package=context_package,
                    selected_ref_descriptors=resolved_selected_ref_descriptors,
                )
                for message in message_rows
            ],
        },
        "tasks": {
            "counts": task_counts,
            "cumulative_estimated_cost_usd": float(cost_total or 0.0),
            "focus": task_focus,
        },
        "attention": attention,
        "context_package": context_package,
        "runtime": runtime_summary,
        "workbench": workbench_state,
        "work_session": work_session,
        "design": {
            "has_design_doc": bool(project.design_doc),
            "revision": ((project.design_doc or {}).get("_meta") or {}).get("revision")
            if isinstance(project.design_doc, dict)
            else None,
        },
        "sha_freshness": sha_freshness,
        "state_revision": _revision,
        "model_integrity": model_integrity,
        "integrity_conflict_trend": integrity_trend,
        "strategic_memory_notes": project.strategic_memory_notes or [],
    }

    # HTTP 304 transmission short-circuit. The full-body ETag covers every
    # volatile field (unlike the narrow state_revision, which only gates the
    # context-package build-skip above), so a matching If-None-Match proves the
    # ENTIRE body is unchanged and we can skip re-transmitting it. Internal
    # direct callers (get_project_strategic_session and friends) pass a throwaway
    # Response and no If-None-Match, so they always receive the full dict — the
    # 304 path is reachable only for a real HTTP GET carrying If-None-Match.
    etag = _compute_strategic_state_etag(result)
    if _if_none_match_hits(if_none_match, etag):
        logger.info(
            "[STRATEGIC_STATE] 304 not modified for project %s "
            "(full-body ETag unchanged)",
            project.id,
        )
        return Response(status_code=304, headers={"ETag": _quote_etag(etag)})
    response.headers["ETag"] = _quote_etag(etag)
    return result


@router.get("/projects/{project_id}/strategic-liveness")
@limiter.limit("60/minute")
async def get_project_strategic_liveness(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Lightweight mode + resident-session liveness probe for reload bootstrap.

    Returns ONLY the cheap fields the chat/workday shell needs to render the mode
    indicator and the resident-session liveness chip immediately on reload:
    ``interaction_mode`` (one indexed workbench row) plus ``resident_session`` /
    ``resident_session_availability`` / ``resident_session_runners`` (an in-memory
    read of ``Project.vision`` + one runner-picker query). It deliberately runs
    NONE of the heavy ``strategic-state`` Phase-1/Phase-2 assembly — no task focus,
    operator attention, cost aggregate, fingerprints, context package, runtime
    summary, event/job feeds, SHA/GitHub freshness, or integrity checks — so it
    resolves in a fraction of the time. The full read model still hydrates
    afterward via ``strategic-state`` and takes over once it lands. Guarded with
    the same ``member``-role project-access check the sibling strategic endpoints
    use.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    # Cheap workbench row (indexed lookup) — the mode indicator's only input.
    workbench_state = await get_strategic_workbench_state(
        session,
        current_user["user_id"],
        project.id,
    )
    # One runner-picker query; the eligible-live count is derived from its
    # per-runner ``eligible`` flags so no heavy runtime summary is assembled.
    resident_runner_options = await build_resident_runner_options(session, project.id)
    eligible_live_runner_count = sum(
        1 for option in resident_runner_options if option.get("eligible")
    )
    return {
        "project_id": str(project.id),
        "interaction_mode": workbench_state.get("interaction_mode", "chat"),
        # In-memory reads off Project.vision — no queries.
        "resident_session": _resident_session_summary(project),
        "resident_session_available": _resident_session_available(),
        "resident_session_availability": _resident_session_availability(
            project,
            eligible_live_runner_count=eligible_live_runner_count,
            runner_options=resident_runner_options,
        ),
        "resident_session_runners": resident_runner_options,
    }


@router.get("/projects/{project_id}/strategic-session")
@limiter.limit("30/minute")
async def get_project_strategic_session(
    request: Request,
    project_id: uuid.UUID,
    include_message_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Resume the persistent Strategic operator session for a project."""

    state = await get_project_strategic_state(
        request,
        project_id,
        # Internal direct call: pass a throwaway Response (headers discarded) and
        # no If-None-Match so the full dict is always returned.
        response=Response(),
        session=session,
        current_user=current_user,
    )
    return await _build_strategic_session_response(
        session,
        project_id,
        state,
        include_message_id=include_message_id,
    )


@router.get("/projects/{project_id}/strategic-conversation")
@limiter.limit("60/minute")
async def get_project_strategic_conversation(
    request: Request,
    project_id: uuid.UUID,
    response: Response,
    include_message_id: uuid.UUID | None = Query(None),
    before_sequence: int | None = Query(None, ge=1),
    after_sequence: int | None = Query(None, ge=0),
    limit: int = Query(_SESSION_CONVERSATION_LIMIT, ge=1, le=100),
    idempotency_key: str | None = Query(None, max_length=255),
    client_idempotency_key: str | None = Query(None, max_length=255),
    search: str | None = Query(None, min_length=2, max_length=200),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the conversation transcript only — no full strategic-state assembly.

    Lighter-weight alternative to GET /strategic-session for polling the
    conversation in the chat surface.  Runs only _recent_session_conversation
    instead of the full read-model assembly.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    lookup_key = (client_idempotency_key or idempotency_key or "").strip() or None
    search = (search or "").strip() or None
    if search and (include_message_id is not None or lookup_key):
        raise HTTPException(
            status_code=400,
            detail="search cannot be combined with a targeted message lookup",
        )
    if client_idempotency_key and idempotency_key and client_idempotency_key != idempotency_key:
        raise HTTPException(status_code=400, detail="Conflicting idempotency identities")
    selectors = sum(
        value is not None
        for value in (before_sequence, after_sequence, include_message_id, lookup_key, search)
    )
    if selectors > 1:
        raise HTTPException(
            status_code=400,
            detail="before, after, target, identity, and search selectors are mutually exclusive",
        )
    matched_message: ProjectMessage | None = None
    if lookup_key:
        matched_message = await _project_message_for_idempotency_key(
            session,
            project_id,
            lookup_key,
        )
        if matched_message is not None and (
            getattr(matched_message, "surface", None) == MessageSurface.CONVERSATION
        ):
            include_message_id = matched_message.id
        else:
            matched_message = None

    if lookup_key:
        conversation = {
            "limit": limit,
            "has_more": False,
            "next_before_sequence": None,
            "next_after_sequence": None,
            "order": "oldest_first",
            "items": [],
        }
    else:
        conversation = await _recent_session_conversation(
            session,
            project_id,
            context_package=None,
            include_message_id=include_message_id,
            before_sequence=before_sequence,
            after_sequence=after_sequence,
            limit=limit,
            search=search,
        )
    result = {
        **conversation,
        "matched_message": (
            _message_preview(matched_message, content_max_chars=None)
            if matched_message is not None
            else None
        ),
        "wakeup_state": (
            (matched_message.context or {}).get("_resident_wakeup_state")
            if matched_message is not None and isinstance(matched_message.context, dict)
            else None
        ),
    }
    # A selected message can already be in the visible window, making the
    # rendered transcript identical to the unpinned one.  Bind the validator
    # to the requested pin independently so an unpinned 304 never suppresses
    # the selected-target request.
    etag_input = {
        **result,
        "selected_message_id": str(include_message_id) if include_message_id else None,
    }
    etag = _compute_strategic_state_etag(etag_input)
    if _if_none_match_hits(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": _quote_etag(etag)})
    response.headers["ETag"] = _quote_etag(etag)
    return result


@router.post("/projects/{project_id}/strategic-session")
@limiter.limit("30/minute")
async def upsert_project_strategic_session(
    request: Request,
    project_id: uuid.UUID,
    payload: StrategicSessionUpdate | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Create or resume a Strategic operator session for an existing project."""

    project = await _get_user_project_with_role(
        project_id,
        current_user["user_id"],
        session,
        "member",
    )
    workbench = payload.workbench if payload is not None else {}
    if workbench_state_size_bytes(workbench) > WORKBENCH_STATE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Workbench state too large (max 50KB)",
        )

    workbench_write = await persist_strategic_workbench_update(
        session,
        current_user["user_id"],
        project_id,
        workbench,
    )

    # Workday (continuous) is the resident-session on-switch: entering it
    # auto-starts the resident session (flag on + a live local runner), leaving
    # it auto-stops. Runs AFTER the mode itself is committed and is fully
    # resilient — a blocked/failed auto-start records a surfaced reason and never
    # breaks this mode-change request.
    await sync_resident_session_for_interaction_mode(
        session,
        project,
        operator_user_id=current_user["user_id"],
        previous_mode=workbench_write.previous_state.get("interaction_mode"),
        new_mode=workbench_write.state.get("interaction_mode"),
    )

    state_response = await get_project_strategic_state(
        request,
        project_id,
        # Internal direct call: pass a throwaway Response (headers discarded) and
        # no If-None-Match so the full dict is always returned.
        response=Response(),
        session=session,
        current_user=current_user,
    )
    response = await _build_strategic_session_response(
        session,
        project_id,
        state_response,
    )
    response["arming"] = workbench_write.arming
    await emit_workbench_refetch(
        project_id,
        resource="strategic_workbench",
        action="updated",
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
        summary_key="workbench",
        summary={
            "interaction_mode": workbench_write.state.get("interaction_mode"),
            "active_surface": workbench_write.state.get("active_surface"),
            "arming": workbench_write.arming,
        },
    )
    return response


@router.post("/projects/{project_id}/strategic-session/commands/{command_id}/execute")
@limiter.limit("30/minute")
async def execute_project_strategic_session_command(
    request: Request,
    project_id: uuid.UUID,
    command_id: str,
    payload: StrategicSessionCommandExecute,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Execute a current server-validated Strategic work-session command."""

    state_response = await get_project_strategic_state(
        request,
        project_id,
        # Internal direct call: pass a throwaway Response (headers discarded) and
        # no If-None-Match so the full dict is always returned.
        response=Response(),
        session=session,
        current_user=current_user,
    )
    duplicate_command_result = await existing_work_session_command_result_for_idempotency_key(
        session,
        project_id=project_id,
        user_id=current_user["user_id"],
        command_id=command_id,
        idempotency_key=payload.idempotency_key,
    )
    if duplicate_command_result is not None:
        response = await _build_strategic_session_response(
            session,
            project_id,
            state_response,
        )
        response["command"] = duplicate_command_result
        return response

    command = _work_session_command_by_id(state_response, command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="Command not found")

    command_result = await execute_work_session_command(
        session,
        project_id=project_id,
        user_id=current_user["user_id"],
        command=command,
        idempotency_key=payload.idempotency_key,
        observed_at_ms=payload.observed_at,
    )

    refreshed_state = await get_project_strategic_state(
        request,
        project_id,
        # Internal direct call: pass a throwaway Response (headers discarded) and
        # no If-None-Match so the full dict is always returned.
        response=Response(),
        session=session,
        current_user=current_user,
    )
    response = await _build_strategic_session_response(
        session,
        project_id,
        refreshed_state,
    )
    response["command"] = command_result
    receipt = (
        command_result.get("receipt")
        if isinstance(command_result.get("receipt"), dict)
        else {}
    )
    if (
        command_result.get("status") in {"succeeded", "failed"}
        and receipt.get("id")
    ):
        await emit_workbench_refetch(
            project_id,
            resource=OPERATOR_COMMAND_EVENT_TYPE,
            action=str(command_result.get("status") or "succeeded"),
            refetch_surfaces=receipt.get("refetch_surfaces")
            if isinstance(receipt.get("refetch_surfaces"), list)
            else (SURFACE_MESSAGES, SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
            summary_key="command",
            summary={
                "command_id": command_result.get("command_id"),
                "label": receipt.get("label"),
                "status": command_result.get("status"),
                "receipt_id": receipt.get("id"),
            },
            message_id=receipt.get("id"),
        )
    return response


async def _project_message_for_idempotency_key(
    session: AsyncSession,
    project_id: uuid.UUID,
    idempotency_key: str,
) -> ProjectMessage | None:
    return (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


@router.post("/projects/{project_id}/strategic-session/check-in")
@limiter.limit("30/minute")
async def create_project_strategic_session_check_in(
    request: Request,
    project_id: uuid.UUID,
    payload: StrategicSessionCheckInCreate | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Record a quiet durable Strategic check-in checkpoint for this operator."""

    await _get_user_project_with_role(
        project_id,
        current_user["user_id"],
        session,
        "member",
    )
    data = payload or StrategicSessionCheckInCreate()
    trimmed_content = (data.content or "").strip()
    if len(trimmed_content) > MESSAGE_CONTENT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Content exceeds {MESSAGE_CONTENT_MAX} character limit",
        )
    if data.workbench_context is not None and workbench_state_size_bytes(
        data.workbench_context
    ) > WORKBENCH_STATE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Workbench context too large (max 50KB)",
        )
    idem_key = (data.idempotency_key or "").strip() or None
    if idem_key and len(idem_key) > 255:
        raise HTTPException(
            status_code=400,
            detail="idempotency_key exceeds 255 characters",
        )

    status = "created"
    message: ProjectMessage | None = None
    if idem_key:
        message = await _project_message_for_idempotency_key(
            session,
            project_id,
            idem_key,
        )
        if message is not None:
            if not _message_is_operator_check_in(message, current_user["user_id"]):
                raise HTTPException(
                    status_code=409,
                    detail="idempotency_key already belongs to another message",
                )
            status = "duplicate"

    if message is None:
        now = _utcnow()
        kind = _compact_text(data.kind, max_chars=40) or "manual"
        workbench_context_for_snapshot = data.workbench_context
        if data.workbench_context is not None:
            workbench_write = await persist_strategic_workbench_update(
                session,
                current_user["user_id"],
                project_id,
                data.workbench_context,
                sync_local_continuation_marker=True,
                arm_interaction_mode=False,
                commit=False,
            )
            workbench_context_for_snapshot = (
                workbench_context_for_strategic_session_snapshot(
                    workbench_write.state,
                    data.workbench_context,
                )
            )
        strategic_session = await build_message_strategic_session_snapshot(
            session,
            current_user["user_id"],
            project_id,
            workbench_context_for_snapshot,
        )
        message_context: Dict[str, Any] = {
            "event_type": _CHECK_IN_EVENT_TYPE,
            "check_in": {
                "version": 1,
                "kind": kind,
                "operator_user_id": current_user["user_id"],
                "captured_at": now.isoformat(),
            },
        }
        if strategic_session:
            message_context["strategic_session"] = strategic_session
        next_sequence = await lock_project_and_next_message_sequence(
            session, project_id
        )
        message = await build_project_message(
            session,
            project_id=project_id,
            actor_user_id=current_user["user_id"],
            sequence=next_sequence,
            surface=MessageSurface.CONVERSATION,
            handling_state=MessageHandlingState.RESPONDED,
            message_type=MessageType.NOTE,
            from_type="system",
            content=trimmed_content or "Checked in with Strategic.",
            context=message_context,
            status="resolved",
            resolved_at=now,
            updated_at=now,
            idempotency_key=idem_key,
        )
        session.add(message)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if idem_key:
                message = await _project_message_for_idempotency_key(
                    session,
                    project_id,
                    idem_key,
                )
                if message is not None:
                    if not _message_is_operator_check_in(
                        message,
                        current_user["user_id"],
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "idempotency_key already belongs to another message"
                            ),
                        ) from None
                    status = "duplicate"
                else:
                    raise
            else:
                raise
        await session.refresh(message)

    await _persist_strategic_check_in_marker(
        session,
        user_id=current_user["user_id"],
        project_id=project_id,
        message=message,
    )

    state_response = await get_project_strategic_state(
        request,
        project_id,
        # Internal direct call: pass a throwaway Response (headers discarded) and
        # no If-None-Match so the full dict is always returned.
        response=Response(),
        session=session,
        current_user=current_user,
    )
    message_preview = await _message_preview_for_session_state(
        session,
        project_id,
        state_response,
        message,
    )
    if status == "created":
        await _persist_message_preview_strategic_session_descriptors(
            session,
            message,
            message_preview,
        )
    session_read_model = await _build_strategic_session_response(
        session,
        project_id,
        state_response,
    )
    session_read_model["check_in"] = {
        "status": status,
        "message_id": str(message.id),
    }
    if status == "created":
        await emit_workbench_refetch(
            project_id,
            resource="strategic_check_in",
            action="created",
            refetch_surfaces=MESSAGE_EVENT_REFETCH_SURFACES,
            summary_key="check_in",
            summary={
                "message_id": str(message.id),
                "status": status,
            },
            message_id=str(message.id),
        )
    return session_read_model


@router.post("/projects/{project_id}/strategic-session/messages")
@limiter.limit("30/minute")
async def create_project_strategic_session_message(
    request: Request,
    project_id: uuid.UUID,
    payload: StrategicSessionMessageCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Record an operator message in the durable Strategic project thread."""

    project = await _get_user_project_with_role(
        project_id,
        current_user["user_id"],
        session,
        "member",
    )

    message_context, _reply_target = await _validated_message_scoped_context(
        session,
        project_id=project.id,
        payload=payload,
    )

    async def _revalidate_message_context(
        locked_session: AsyncSession,
        message_id: uuid.UUID,
        current_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        revalidated_context, _ = await _validated_message_scoped_context(
            locked_session,
            project_id=project.id,
            payload=payload,
            lock_references=True,
        )
        if revalidated_context != message_context:
            raise HTTPException(
                status_code=409,
                detail="Message references changed before commit; refresh and retry",
            )
        return await _consume_message_attachment_uploads(
            locked_session,
            project_id=project.id,
            actor_user_id=current_user["user_id"],
            message_id=message_id,
            context=current_context,
        )

    result = await create_operator_project_message(
        session,
        project=project,
        user_id=current_user["user_id"],
        message_type=parse_operator_message_type(payload.message_type),
        content=(
            payload.content
            if payload.content.strip()
            or not _message_context_has_attachments(message_context)
            else "Sent attachment."
        ),
        context=message_context,
        workbench_context=payload.workbench_context,
        idempotency_key=payload.idempotency_key,
        persist_empty_workbench_context=False,
        sync_local_continuation_marker=False,
        surface=MessageSurface.CONVERSATION,
        reply_to_message_id=payload.reply_to_message_id,
        include_strategic_session_snapshot=False,
        precommit_validate=_revalidate_message_context,
    )
    # The send receipt is intentionally lightweight. The canonical message has
    # all visible chips/reply metadata needed for reconciliation; fetching the
    # full strategic-state/runtime model here made send latency scale with the
    # entire project and duplicated data the client refreshes independently.
    message_preview = _message_preview(result.message, content_max_chars=None)
    response = {
        "id": result.message_id,
        "status": result.status,
        "arming": result.arming,
        "wakeup_state": result.wakeup_state,
        "message": message_preview,
        "project_id": str(project.id),
    }
    if result.message_event is not None:
        await broadcast_to_project(
            str(project_id),
            "message_created",
            result.message_event,
        )
    return response


class MessageAttachmentUploadUrlRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=128)
    size_bytes: int = Field(..., ge=1, le=_MESSAGE_ATTACHMENT_MAX_BYTES)


@router.get(
    "/projects/{project_id}/strategic-conversation/{message_id}/attachments/"
    "{attachment_id}/download-url"
)
@limiter.limit("60/minute")
async def get_conversation_attachment_download_url(
    request: Request,
    project_id: uuid.UUID,
    message_id: uuid.UUID,
    attachment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Resolve one authorized durable attachment identity to a fresh URL."""

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    message = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.id == message_id,
                ProjectMessage.project_id == project_id,
                ProjectMessage.surface == MessageSurface.CONVERSATION,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=404, detail="Conversation message not found")
    context = message.context if isinstance(message.context, dict) else {}
    raw_attachments = context.get("attachments")
    attachment: Dict[str, Any] | None = None
    if isinstance(raw_attachments, list):
        for candidate in raw_attachments[:12]:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("attachment_id") or candidate.get("id")) != str(attachment_id):
                continue
            try:
                attachment = _canonical_message_attachment(candidate, project_id)
            except HTTPException:
                logger.warning(
                    "[ATTACHMENT] invalid durable descriptor on message %s",
                    message_id,
                )
                break
            break
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    try:
        download_url = await generate_download_signed_url(
            attachment["storage_uri"], expiration_minutes=15
        )
    except Exception as exc:
        logger.warning(
            "[ATTACHMENT] lazy download URL failed for message %s: %s",
            message_id,
            exc,
        )
        raise HTTPException(status_code=503, detail="Storage unavailable") from exc
    return {
        "attachment_id": str(attachment_id),
        "download_url": download_url,
        "expires_in_seconds": 900,
    }


@router.post("/projects/{project_id}/messages/attachment-upload-url")
@limiter.limit("60/minute")
async def get_message_attachment_upload_url(
    request: Request,
    project_id: uuid.UUID,
    req: MessageAttachmentUploadUrlRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Generate a signed upload URL for a message attachment."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member",
    )
    safe_filename = _sanitize_message_attachment_filename(req.filename)
    content_type = _attachment_content_type(req.content_type)
    attachment_id = str(uuid.uuid4())
    object_name = f"message-attachments/{project_id}/{attachment_id}/{safe_filename}"
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    def _generate() -> str:
        blob = (
            gcs_storage.Client(project=GCP_PROJECT_ID)
            .bucket(ARTIFACT_BUCKET_NAME)
            .blob(object_name)
        )
        return _generate_v4_signed_url(
            blob,
            version="v4",
            expiration=_dt.timedelta(minutes=_MESSAGE_ATTACHMENT_UPLOAD_EXPIRY_MINUTES),
            method="PUT",
            content_type=content_type,
            headers={"Content-Length": str(req.size_bytes)},
        )

    try:
        upload_url = await asyncio.to_thread(_generate)
    except Exception as exc:
        logger.warning(
            "[ATTACHMENT] signed upload URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(status_code=503, detail="Storage unavailable") from exc

    try:
        download_url = await generate_download_signed_url(
            storage_uri,
            expiration_minutes=60,
        )
    except Exception as exc:
        logger.warning(
            "[ATTACHMENT] signed download URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(status_code=503, detail="Storage unavailable") from exc

    expires_at = _utcnow() + _dt.timedelta(
        minutes=_MESSAGE_ATTACHMENT_UPLOAD_EXPIRY_MINUTES
    )
    receipt = ProjectMessageAttachmentUpload(
        attachment_id=uuid.UUID(attachment_id),
        project_id=project_id,
        actor_user_id=current_user["user_id"],
        storage_uri=storage_uri,
        filename=safe_filename,
        content_type=content_type,
        expected_size_bytes=req.size_bytes,
        expires_at=expires_at,
    )
    session.add(receipt)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await delete_storage_uri_best_effort(storage_uri)
        raise
    background_tasks.add_task(
        _cleanup_expired_message_attachment_uploads,
        project_id,
        current_user["user_id"],
    )

    return {
        "upload_url": upload_url,
        "download_url": download_url,
        "storage_uri": storage_uri,
        "attachment_id": attachment_id,
        "content_type": content_type,
        "size_bytes": req.size_bytes,
        "expires_at": expires_at.isoformat(),
    }


@router.delete(
    "/projects/{project_id}/messages/attachment-uploads/{attachment_id}"
)
@limiter.limit("60/minute")
async def delete_message_attachment_upload(
    request: Request,
    project_id: uuid.UUID,
    attachment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Explicitly clean up one unconsumed or expired attachment upload."""

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    receipt = (
        await session.execute(
            select(ProjectMessageAttachmentUpload)
            .where(
                ProjectMessageAttachmentUpload.attachment_id == attachment_id,
                ProjectMessageAttachmentUpload.project_id == project_id,
                ProjectMessageAttachmentUpload.actor_user_id
                == current_user["user_id"],
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if receipt is None:
        return {"status": "not_found", "deleted": False}
    if receipt.consumed_at is not None or receipt.message_id is not None:
        raise HTTPException(status_code=409, detail="Consumed attachment cannot be deleted")
    try:
        deleted = await asyncio.wait_for(
            delete_storage_uri_best_effort(receipt.storage_uri),
            timeout=_MESSAGE_ATTACHMENT_CLEANUP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        deleted = False
    if not deleted:
        raise HTTPException(status_code=503, detail="Storage cleanup unavailable")
    await session.delete(receipt)
    await session.commit()
    return {"status": "deleted", "deleted": True}


@router.post("/projects/{project_id}/strategic-session/start")
@limiter.limit("30/minute")
async def start_project_strategic_resident_session(
    request: Request,
    project_id: uuid.UUID,
    payload: StrategicResidentSessionStart | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Start (or adopt) a resident Strategic session on the operator's runner.

    Gated on the ``strategic_session_enabled`` runtime setting (default off);
    the resident session subsumes the legacy cadence for its project while live.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    if not runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING, False):
        raise HTTPException(
            status_code=403,
            detail="Strategic resident sessions are disabled",
        )
    data = payload or StrategicResidentSessionStart()
    snapshot = await start_resident_session(
        session,
        project,
        operator_user_id=current_user["user_id"],
        runner_id=data.runner_id,
    )
    return {"project_id": str(project_id), "enabled": True, "session": snapshot}


@router.post("/projects/{project_id}/strategic-session/stop")
@limiter.limit("30/minute")
async def stop_project_strategic_resident_session(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Stop the resident Strategic session and hand authority back to the cadence.

    Not gated on the feature flag: an operator can always reclaim/stop a session
    (instant manual reclaim), even after the flag is turned off.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    snapshot = await stop_resident_session(
        session,
        project,
        operator_user_id=current_user["user_id"],
    )
    return {"project_id": str(project_id), "session": snapshot}
