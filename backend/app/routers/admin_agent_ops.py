"""Agent operational endpoints: design-doc, playbook, annotate, strategic.

These handlers were extracted from app.main to keep that module under its
size cap. All HTTP routes, request/response schemas, and authentication rules
are unchanged.
"""

import hashlib
import asyncio
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import func as _func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.alert_dispatch import (
    _maybe_create_problem_model_live_core_alert,
    _maybe_create_problem_model_wipe_alert,
)
from app.broadcast import broadcast_to_project
from app.capability_proof import carry_proof_records_for_consolidation
from app.capability_trial_verdict import (
    carry_adoption_verdicts_for_consolidation,
    record_trial_verdict,
)
from app.direction_referential_guard import build_retired_direction_grounding
from app.database import get_session
from app.knowledge_query import fetch_retirement_memo, get_knowledge_document
from app.strategic_events import (
    STRATEGIC_KIND_DIRECTIONS,
    STRATEGIC_KIND_PROBLEM_MODEL,
    STRATEGIC_KIND_RUNNING_UPDATE,
    emit_strategic_updated,
)
from app.strategic_op_contracts import (
    normalize_problem_model_envelope,
    normalize_update_direction,
)
from app.direction_lifecycle import (
    _reset_outcome_detection_state,
    apply_churn_retirement_keep,
    apply_churn_retirement_retire,
    direction_has_churn_retirement_recommendation,
    reactivate_paused_direction,
)
from app.models import (
    KnowledgeDocument,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    SourceKind,
    StrategicSpawnRequest,
    Task,
    TASK_OUTCOME_TERMINAL_STATUSES,
    TaskStatus,
)
from app.message_audience import stamp_message_audience
from app.project_message_creation import (
    build_project_message,
    lock_project_and_next_message_sequence,
)
from app.payload_schemas import (
    ALL_TASK_TYPES as _VALID_TASK_TYPES,
    validate_internal_payload as _validate_internal_payload,
)
from app.playbook_storage import (
    _PLAYBOOK_CAPACITY_BYTES,
    _playbook_size_bytes,
    visible_playbook_entry,
    maybe_archive_for_capacity as _maybe_archive_for_capacity,
)
from app.project_message_projection import (
    message_event_payload,
    message_updated_event_payload,
)
from app.routers.agent import verify_agent_token
from app.source_ref_validation import (
    compute_message_citation_warning as _compute_message_citation_warning,
    ground_design_doc_value,
    resolve_branch_verification_context as _resolve_branch_verification_context,
)
from app.direction_grounding import validate_create_direction_grounding
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    limiter,
    runtime_config as _runtime_config,
)
from app.size_limit_policy import bump_size_limit as _bump_size_limit_policy
from app.size_limit_policy import classify_breach as _classify_breach_policy
from app.strategic_completion import (
    _find_matching_direction,
    _normalize_recommended_mode,
    _reset_scaffolding_state_on_outcome_change,
    _spawn_retirement_memos,
    record_retired_direction_memory,
    STRATEGIC_CHALLENGE_CONTEXT_KEY,
    find_gap_respawn_match,
)
from app.resource_envelope import resolve_project_require_local_runner
from app.strategic_resident_session import (
    read_resident_session as _read_resident_session,
    resident_session_is_active as _resident_session_is_active,
)
from app.strategic_watches import (
    AcknowledgeWatchInput,
    CancelWatchInput,
    WatchTaskInput,
    acknowledge_watch,
    cancel_watch,
    register_task_watch,
    serialize_watch,
)
from app.workbench_state import (
    strategic_local_continuation_status_from_vision,
)
from app.strategic_constants import (
    MIN_HEALTHY_ACTIVE_DIRECTIONS,
    STRATEGIC_DIRECTION_TEXT_MAX,
    STRATEGIC_MESSAGE_CONTENT_MAX,
    STRATEGIC_NOTES_HISTORY_MAX,
    STRATEGIC_RATIONALE_MAX,
    STRATEGIC_RESOLUTION_MAX,
    STRATEGIC_SYSTEM_ID_MAX,
)
from app.task_capabilities import (
    STRATEGIC_TASK_TYPES,
    STRATEGIST_CONTEXTUAL_TASK_TYPES,
    STRATEGIST_SPAWNABLE_TASK_TYPES,
)
from app.strategic_spawn_requests import (
    cancel_deferred_spawn_request,
    submit_strategic_spawn,
)
from app.auth import require_admin
from app.task_locking import (
    DESIGN_DOC_MAX_BYTES,
    DesignDocSizeLimitExceeded,
    _RESERVED_DESIGN_DOC_KEYS,
    lock_project_or_429 as _lock_project_or_429,
    task_lock,
    with_design_doc_locked as _with_design_doc_locked,
    with_directions_locked as _with_directions_locked,
    with_vision_locked as _with_vision_locked,
)
from app.time_utils import _utcnow

router = APIRouter()

logger = logging.getLogger(__name__)

# _RESERVED_DESIGN_DOC_KEYS is imported from app.task_locking — the
# authoritative definition lives alongside the size gate in that module.

# Mixed surface marker: this module deliberately hosts both /api/admin and
# /api/agent routes. The route-table invariant in backend/testing/test_invariants.py
# uses this marker to include the sibling /api/agent endpoints in the same
# auth scan as the admin routes.
ADMIN_AGENT_OPS_MODULE = __name__
STRATEGIC_MESSAGE_CONTEXT_MAX_BYTES = 50_000
RESIDENT_REPLY_COVERED_MESSAGE_MAX = 20

# Canonical set of strategic ops the agent script advertises and dispatches.
# Derived from the same inventory as the agent-side STRATEGIC_OP_INVENTORY
# (agent/src/handlers/strategic-op-inventory.ts). The parity test in
# backend/testing/test_admin_routers.py asserts every name here has a real
# elif handler in this module, so adding an op to the inventory and forgetting
# the backend handler produces a CI failure rather than a silent no-op.
STRATEGIC_AGENT_OPS: frozenset[str] = frozenset({
    "set_problem_model",
    "patch_problem_model",
    "replace_problem_model",
    "set_running_update",
    "set_memory",
    "set_session_summary",
    "reflect",
    "pending_confirm",
    "resolve_pending_confirm",
    "set_vision",
    "create_direction",
    "update_direction",
    "retire_direction",
    "stage_direction",
    "approve_direction",
    "reject_direction",
    "activate_direction",
    "create_message",
    "resolve_message",
    "spawn_task",
    "spawn_fix",
    "cancel_task",
    "watch_task",
    "ack_watch",
    "cancel_watch",
    "dispose_completion_debt",
    "set_retirement_critiques",
    "set_evidence_progress",
    "set_acceptance_criteria",
    "record_finding_resolution",
    "consume_knowledge_followup",
    "consume_knowledge",
    "dismiss_knowledge_followup",
    "dismiss_knowledge",
    "request_qualification",
})


def _cap_str(
    value: Any, max_len: int, field_name: str, warnings: List[str] | None = None
) -> str:
    """Cap a string field, appending a human-readable warning if truncation occurred."""
    s = str(value) if value is not None else ""
    if len(s) > max_len:
        if warnings is not None:
            warnings.append(
                f"{field_name} was truncated from {len(s)} to {max_len} chars — "
                f"resubmit with content under {max_len} chars to avoid losing text"
            )
        return s[:max_len]
    return s


def _cap_list(
    lst: list, max_items: int, field_name: str, warnings: List[str] | None = None
) -> list:
    """Cap a list to the last N items, warning if older items were dropped."""
    if len(lst) > max_items:
        dropped = len(lst) - max_items
        if warnings is not None:
            warnings.append(
                f"{field_name} kept only the newest {max_items} of {len(lst)} items — "
                f"{dropped} oldest item(s) were dropped"
            )
        return lst[-max_items:]
    return lst


def _coerce_criterion_keys_payload(raw: Any, field_name: str) -> list[str]:
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a list of criterion_key strings",
        )
    keys: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(
                status_code=400,
                detail=f"{field_name}[{idx}] must be a non-empty string",
            )
        keys.append(item.strip())
    return list(dict.fromkeys(keys))


async def _validate_criterion_keys_payload(
    session: AsyncSession,
    project_id: uuid.UUID,
    raw: Any,
    field_name: str,
) -> list[str]:
    keys = _coerce_criterion_keys_payload(raw, field_name)
    if not keys:
        return []

    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    from app.direction_referential_guard import (
        check_criterion_key_write,
        load_version_criterion_key_sets,
    )

    active_keys, inactive_keys = await load_version_criterion_key_sets(
        session, project_id, open_version.id
    )
    failures: dict[str, list[str]] = {}
    for key in keys:
        classification, _ = check_criterion_key_write(
            key, active_keys, inactive_keys
        )
        if classification is not None:
            failures.setdefault(classification, []).append(key)
    if failures:
        classification = "dangling" if "dangling" in failures else "inactive"
        raise HTTPException(
            status_code=400,
            detail={
                "error": "criterion_key_invalid",
                "classification": classification,
                "keys": failures[classification],
                "active_criterion_keys": sorted(active_keys),
            },
        )
    return keys


async def _backfill_direction_merge_evidence_after_mapping(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str,
) -> None:
    try:
        from app import acceptance_criteria as ac

        recorded = await ac.backfill_direction_merge_evidence(
            session, project_id, direction_key
        )
        if recorded:
            await session.commit()
    except Exception:  # noqa: BLE001 - best-effort post-mapping repair
        await session.rollback()
        logger.warning(
            "[STRATEGIC API] criterion_keys backfill failed for direction %s",
            direction_key,
            exc_info=True,
        )


class DesignDocUpdate(BaseModel):
    """Incremental design doc page update from an agent."""

    task_id: uuid.UUID
    op: str  # "set", "delete", "list"
    key: str | None = None
    value: Any | None = None


def _get_file_list_from_system_page(doc_val: dict) -> list[str]:
    """Extract the file path list from a ``systems/*`` design-doc page.

    Systems pages were authored with different field-name conventions for
    listing their covered files: ``key_paths`` (majority convention),
    ``key_files`` (a few pages), or ``modules`` (earliest pages).  This helper
    checks each in preference order and returns entries from the first one that
    is a non-empty list of strings, so the caller (the ``_file_to_system``
    reverse-index builder) never depends on a single unenforced field name.
    """
    for field in ("key_paths", "key_files", "modules"):
        files = doc_val.get(field)
        if isinstance(files, list):
            return [f for f in files if isinstance(f, str)]
    return []


async def backfill_file_to_system_index(session: AsyncSession) -> int:
    """One-time startup backfill: recompute ``_file_to_system`` for every
    project that has a non-empty ``design_doc``.

    Uses :func:`_with_design_doc_locked` for each project so the canonical
    locking helper is used for all ``project.design_doc`` writes.

    The reverse-index was originally built only from the ``modules`` field
    name, but most ``systems/*`` pages use ``key_paths`` (or ``key_files``).
    This sweep applies the corrected field-reading logic (via
    :func:`_get_file_list_from_system_page`) to every existing systems page,
    bringing the index in sync without waiting for each page to be re-set.

    Uses cursor-based pagination (50 projects per batch) to avoid loading all
    project IDs into memory and to release per-project locks promptly between
    batches rather than accumulating them in a single long-running transaction.

    Returns the number of projects whose index was actually updated.
    """
    import uuid as _uuid

    updated_count = 0
    last_cursor: _uuid.UUID = _uuid.UUID("00000000-0000-0000-0000-000000000000")
    batch_size = 50

    while True:
        batch_ids = (
            (
                await session.execute(
                    select(Project.id)
                    .where(
                        Project.design_doc.isnot(None),
                        Project.design_doc != {},
                        Project.id > last_cursor,
                    )
                    .order_by(Project.id)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not batch_ids:
            break

        for project_id in batch_ids:
            did_update = await _backfill_one_project(session, project_id)
            await session.commit()
            if did_update:
                updated_count += 1

        last_cursor = batch_ids[-1]

    return updated_count


async def _backfill_one_project(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Recompute ``_file_to_system`` for a single project under a design_doc lock.

    Returns True if the index was updated, False if it was already correct or
    the project had no systems pages.
    """
    updated = False

    def _mutate(doc: dict) -> None:
        nonlocal updated
        rebuilt: dict[str, str] = {}
        for doc_key, doc_val in doc.items():
            if doc_key.startswith("systems/") and isinstance(doc_val, dict):
                sys_id = doc_key[len("systems/") :]
                for fpath in _get_file_list_from_system_page(doc_val):
                    rebuilt[fpath] = sys_id
        current = doc.get("_file_to_system")
        if isinstance(current, dict) and current == rebuilt:
            return  # already correct
        doc["_file_to_system"] = rebuilt
        updated = True

    try:
        await _with_design_doc_locked(session, project_id, _mutate)
    except DesignDocSizeLimitExceeded as exc:
        logger.warning(
            "[backfill_file_to_system_index] skipped project %s: %s",
            project_id,
            exc,
        )
        updated = False
    return updated


@router.post("/api/agent/design-doc")
@limiter.limit("60/minute")
async def update_design_doc(
    request: Request,
    payload: DesignDocUpdate,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent endpoint for design doc operations. Read (list/get) is open to all agents.
    Write (set/delete) is restricted to design_doc_update/strategic_review agents."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Write operations restricted to strategic agents; reads open to all
    if (
        payload.op not in ("list", "get")
        and token.get("task_type") not in STRATEGIC_TASK_TYPES
    ):
        raise HTTPException(
            status_code=403,
            detail="Write operations restricted to design_doc_update/strategic_review agents",
        )

    if payload.op == "list":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        return {
            "keys": [
                k
                for k in doc
                if not k.startswith("_")
                and not k.startswith("playbook/")
                and k not in _RESERVED_DESIGN_DOC_KEYS
            ]
        }

    if payload.op == "get":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        if not payload.key:
            raise HTTPException(status_code=400, detail="key is required for get")
        value = doc.get(payload.key)
        if value is None:
            raise HTTPException(
                status_code=404, detail=f"Key '{payload.key}' not found"
            )
        # Ground presence against the project's real integration branch via the
        # GitHub API — not the orchestrator's local checkout, which is absent in
        # prod/CI and would silently degrade this reconcile to a no-op. Mirrors
        # the finding-ingestion and strategic-payload grounding paths
        # (strategic_spawning.py / autonomous_research.py). Falls back to local
        # git HEAD when credentials/branch are unavailable.
        verification_kwargs = (
            await _resolve_branch_verification_context(session, project)
            if project
            else {}
        )
        value = await ground_design_doc_value(value, payload.key, **verification_kwargs)
        return {"key": payload.key, "value": value}

    if not payload.key or not isinstance(payload.key, str):
        raise HTTPException(status_code=400, detail="key is required for set/delete")
    key = payload.key.strip()
    if not key or len(key) > 100 or key.startswith("_"):
        raise HTTPException(
            status_code=400, detail="Invalid key (max 100 chars, no _ prefix)"
        )
    if key.startswith("playbook/") or key in _RESERVED_DESIGN_DOC_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Key '{key}' is reserved. Use the /api/agent/playbook endpoint for playbook/* keys.",
        )

    # Lock project for atomic update (skip_locked + 429 avoids lock_timeout cascade)
    project = await _lock_project_or_429(session, task.project_id)

    doc = dict(project.design_doc or {})
    meta = doc.get("_meta", {"revision": 0})

    if payload.op == "set":
        if payload.value is None:
            raise HTTPException(status_code=400, detail="value is required for set")
        # Size check: individual value max 50KB, total doc max 500KB
        import json as _json

        val_size = len(_json.dumps(payload.value))
        if val_size > 50_000:
            raise HTTPException(
                status_code=400, detail=f"Value too large ({val_size} bytes, max 50KB)"
            )
        # Measure only agent-facing keys: reserved keys have their own internal
        # retention caps and must not consume the agent-write budget.
        old_agent_keys_doc = {
            k: v for k, v in doc.items() if k not in _RESERVED_DESIGN_DOC_KEYS
        }
        old_size = len(_json.dumps(old_agent_keys_doc))
        doc[key] = payload.value
        new_agent_keys_doc = {
            k: v for k, v in doc.items() if k not in _RESERVED_DESIGN_DOC_KEYS
        }
        total_size = len(_json.dumps(new_agent_keys_doc))
        # Allow writes that shrink or don't grow the corpus even when already over cap
        # (matches the canonical with_design_doc_locked logic in task_locking.py).
        if total_size > DESIGN_DOC_MAX_BYTES and total_size > old_size:
            del doc[key]  # rollback
            raise HTTPException(
                status_code=400,
                detail=f"Total design doc too large ({total_size} bytes, max 500KB)",
            )
        # Rebuild file-to-system reverse index atomically when a systems/* key is set
        if key.startswith("systems/"):
            file_to_system_map: dict[str, str] = {}
            for doc_key, doc_val in doc.items():
                if doc_key.startswith("systems/") and isinstance(doc_val, dict):
                    sys_id = doc_key[len("systems/") :]
                    for fpath in _get_file_list_from_system_page(doc_val):
                        file_to_system_map[fpath] = sys_id
            doc["_file_to_system"] = file_to_system_map
    elif payload.op == "delete":
        doc.pop(key, None)
        # Rebuild reverse index if a systems/* key is deleted to remove stale entries
        if key.startswith("systems/"):
            file_to_system_deleted_map: dict[str, str] = {}
            for doc_key, doc_val in doc.items():
                if doc_key.startswith("systems/") and isinstance(doc_val, dict):
                    sys_id = doc_key[len("systems/") :]
                    for fpath in _get_file_list_from_system_page(doc_val):
                        file_to_system_deleted_map[fpath] = sys_id
            doc["_file_to_system"] = file_to_system_deleted_map
    else:
        raise HTTPException(status_code=400, detail=f"Unknown op: {payload.op}")

    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by_task"] = str(task.id)
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    return {"status": "ok", "op": payload.op, "key": key, "revision": meta["revision"]}


# ── Agent Playbook ───────────────────────────────────────────────────────
# Any agent task type can read/write persistent operational knowledge stored
# as playbook/* keys in the project's design_doc JSONB column.


class PlaybookUpdate(BaseModel):
    task_id: uuid.UUID
    op: str  # "get", "set", "list"
    key: str | None = None  # alphanumeric + hyphens, max 50 chars
    value: str | None = None  # plain text, max 2KB
    applicable_types: List[str] | None = (
        None  # task types this entry applies to; None = all types
    )
    branch: str | None = None  # git branch the entry was authored on (provenance)
    head_sha: str | None = None  # short HEAD sha at write time (provenance)


_PLAYBOOK_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,49}$")


@router.post("/api/agent/playbook")
@limiter.limit("120/minute")
async def update_playbook(
    request: Request,
    payload: PlaybookUpdate,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent endpoint for persistent operational playbook. Any task type may call this."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
    # NOTE: unlike design-doc, no task_type restriction — any agent may use playbook

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    op = payload.op
    if op == "delete":
        raise HTTPException(
            status_code=403, detail="Playbook delete is not allowed for agents"
        )
    if op not in ("get", "set", "list"):
        raise HTTPException(status_code=400, detail=f"Unknown op: {op}")

    if op == "list":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        my_id = str(task.id)
        keys = []
        for k, v in doc.items():
            if not k.startswith("playbook/"):
                continue
            if visible_playbook_entry(v, viewer_task_id=my_id) is None:
                continue
            keys.append(k[len("playbook/") :])
        return {"status": "ok", "op": op, "keys": keys}

    if op == "get":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        my_id = str(task.id)

        # Single-key scoped retrieval
        if payload.key and payload.key.strip():
            full_key = f"playbook/{payload.key.strip()}"
            v = doc.get(full_key) if doc else None
            v = visible_playbook_entry(v, viewer_task_id=my_id)
            if v is not None:
                return {
                    "status": "ok",
                    "op": op,
                    "entries": {
                        payload.key.strip(): {
                            "content": v.get("content", ""),
                            "updated_at": v.get("updated_at", ""),
                            "source_task_id": v.get("source_task_id", ""),
                            "branch": v.get("branch", ""),
                            "head_sha": v.get("head_sha", ""),
                        }
                    },
                }
            return {"status": "ok", "op": op, "entries": {}}

        # No specific key — return all entries (existing behavior)
        entries = {}
        for k, v in doc.items():
            if k.startswith("playbook/"):
                v = visible_playbook_entry(v, viewer_task_id=my_id)
                if v is None:
                    continue
                short_key = k[len("playbook/") :]
                entries[short_key] = {
                    "content": v.get("content", ""),
                    "updated_at": v.get("updated_at", ""),
                    "source_task_id": v.get("source_task_id", ""),
                    "branch": v.get("branch", ""),
                    "head_sha": v.get("head_sha", ""),
                }
        return {"status": "ok", "op": op, "entries": entries}

    # set requires a valid key
    if not payload.key:
        raise HTTPException(status_code=400, detail="key is required for set")
    key = payload.key.strip()
    if key.startswith("_") or not _PLAYBOOK_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="Invalid key: must be alphanumeric+hyphens, max 50 chars, no _ prefix",
        )

    # Lock project for atomic update (skip_locked + 429 avoids lock_timeout cascade)
    project = await _lock_project_or_429(session, task.project_id)

    import json as _json

    doc = dict(project.design_doc or {})
    meta = doc.get("_meta", {"revision": 0})

    if op == "set":
        if payload.value is None:
            raise HTTPException(status_code=400, detail="value is required for set")
        value_size = len(payload.value.encode("utf-8"))
        if value_size > 2048:
            raise HTTPException(
                status_code=400,
                detail=f"Value too large ({value_size} bytes, max 2KB)",
            )
        if payload.applicable_types is not None:
            invalid = [
                t for t in payload.applicable_types if t not in _VALID_TASK_TYPES
            ]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid task type(s): {invalid}. Must be one of: {sorted(_VALID_TASK_TYPES)}",
                )
        playbook_key = f"playbook/{key}"
        existing = doc.get(playbook_key, {})
        if not isinstance(existing, dict):
            existing = {}
        existing_pending_source = existing.get("pending_source")
        if existing_pending_source and existing_pending_source != str(task.id):
            raise HTTPException(
                status_code=409,
                detail="Another active task has a pending update for this playbook key",
            )
        entry: dict = {
            "content": payload.value,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": existing.get("confirmed_count", 0),
        }
        if payload.applicable_types is not None:
            entry["applicable_types"] = payload.applicable_types
        # Provenance: stamp branch + short HEAD sha captured at write time.
        # Stored only when supplied — absent leaves a legacy entry untouched,
        # fully backward compatible. Each set overwrites with current truth.
        if payload.branch:
            entry["branch"] = payload.branch[:200]
        if payload.head_sha:
            entry["head_sha"] = payload.head_sha[:40]
        # Anti-poisoning: until the source task transitions to COMPLETED,
        # the entry is invisible to other tasks (filtered in the get/list
        # paths above and in prompt builders that surface playbook entries).
        # On COMPLETED, task_lifecycle clears the pending_source field; on
        # FAILED/CANCELLED, the entry is removed entirely. Prevents a task
        # that mid-flight claims "X exists" from poisoning peers' prompts
        # when the task ultimately fails to land the code.
        if task.status != TaskStatus.COMPLETED:
            entry["pending_source"] = str(task.id)
            # Updating a committed entry is staged, not destructive. If this
            # task later fails, finalization restores the prior shared truth
            # instead of deleting the key entirely. Repeated writes by the same
            # task retain the original committed value.
            prior_entry = (
                existing.get("_pending_previous")
                if existing_pending_source == str(task.id)
                else existing
            )
            if isinstance(prior_entry, dict) and prior_entry:
                entry["_pending_previous"] = prior_entry
        # Capacity accounting replaces this key; do not count the old value and
        # the staged replacement simultaneously.
        doc.pop(playbook_key, None)
        # Evict least-useful entries until there is room for the new write.
        # Loop so a single large write or an already-over-cap state self-heals.
        entry_estimate = len(_json.dumps(entry).encode())
        while _playbook_size_bytes(doc) + entry_estimate > _PLAYBOOK_CAPACITY_BYTES:
            size_before = _playbook_size_bytes(doc)
            doc = _maybe_archive_for_capacity(doc, entry_estimate)
            if _playbook_size_bytes(doc) >= size_before:
                break  # nothing evictable; size didn't shrink
        doc[playbook_key] = entry
        # Final hard-cap check — only triggers when nothing was evictable.
        playbook_total = _playbook_size_bytes(doc)
        if playbook_total > _PLAYBOOK_CAPACITY_BYTES:
            del doc[playbook_key]  # rollback
            raise HTTPException(
                status_code=400,
                detail=f"Playbook too large ({playbook_total} bytes, max {_PLAYBOOK_CAPACITY_BYTES} bytes) and no evictable entries",
            )
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by_task"] = str(task.id)
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    return {"status": "ok", "op": op, "key": key, "revision": meta["revision"]}


# ── Agent Annotations ───────────────────────────────────────────────────
# Any agent can leave comments, issues, or vision suggestions.
# Stored as ProjectMessages so design-doc and strategic agents pick them up
# via the existing open_messages flow.


class AgentAnnotation(BaseModel):
    task_id: uuid.UUID
    annotation_type: Literal["comment", "issue", "vision", "size_breach"]
    content: str = ""  # free-form prose (comment/issue/vision); ignored for size_breach
    target_page: str | None = None  # design doc page key (for comments)
    severity: str | None = None  # low/medium/high (for issues)
    # size_breach fields — programmatic signal from a test-invariant failure.
    # Server bumps the limit (policy-driven, not agent-judgment) and returns
    # new_limit; the agent applies it to module_size_limits.json and commits.
    # For chronic offenders the server refuses the silent bump and returns
    # is_chronic=True without new_limit; pass force_chronic_override=True with
    # explicit operator approval to grant the bump anyway.
    file_path: str | None = None
    current_size: int | None = None
    change_summary: str | None = None
    section: str = "backend"  # which module_size_limits.json section the file lives in
    force_chronic_override: bool = (
        False  # operator-approval bypass for chronic offenders
    )


_ANNOTATION_TYPE_MAP = {
    "comment": MessageType.NOTE,
    "issue": MessageType.BUG_REPORT,
    "vision": MessageType.FEEDBACK,
    "size_breach": MessageType.FEEDBACK,
}


def _bump_size_limit(current_size: int) -> int:
    """Server-side policy for how much to bump a size limit on breach.

    Delegates to the shared, dependency-free policy in
    ``app.size_limit_policy`` so the server endpoint and the standalone
    ``check_size_limits.py`` checker produce identical bumps. Kept as a thin
    wrapper here (and re-exported from ``app.main``) so existing call sites
    and test patches (test_agent_annotate.py) keep working unchanged."""
    return _bump_size_limit_policy(current_size)


@router.post("/api/agent/annotate")
@limiter.limit("30/minute")
async def agent_annotate(
    request: Request,
    payload: AgentAnnotation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Any agent can leave annotations — comments, issues, or vision suggestions."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    is_size_breach = payload.annotation_type == "size_breach"
    if is_size_breach:
        if not payload.file_path or not payload.file_path.strip():
            raise HTTPException(
                status_code=400, detail="size_breach requires file_path"
            )
        if not payload.current_size or payload.current_size <= 0:
            raise HTTPException(
                status_code=400, detail="size_breach requires positive current_size"
            )
        if not payload.change_summary or not payload.change_summary.strip():
            raise HTTPException(
                status_code=400, detail="size_breach requires change_summary"
            )
        if len(payload.file_path) > 200:
            raise HTTPException(status_code=400, detail="file_path too long")
        if len(payload.change_summary) > 500:
            raise HTTPException(
                status_code=400, detail="change_summary exceeds 500 character limit"
            )
    else:
        if len(payload.content) > 2000:
            raise HTTPException(
                status_code=400, detail="Content exceeds 2000 character limit"
            )
        if not payload.content.strip():
            raise HTTPException(status_code=400, detail="Content cannot be empty")
    if payload.severity and payload.severity not in ("low", "medium", "high"):
        raise HTTPException(
            status_code=400, detail="Severity must be low, medium, or high"
        )
    if payload.target_page and len(payload.target_page) > 100:
        raise HTTPException(status_code=400, detail="Target page key too long")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Cap: max 10 annotations per task
    count_stmt = (
        select(_func.count())
        .select_from(ProjectMessage)
        .where(
            ProjectMessage.from_task_id == task.id,
            ProjectMessage.from_type == "agent",
        )
    )
    count = (await session.execute(count_stmt)).scalar() or 0
    if count >= 10:
        raise HTTPException(status_code=429, detail="Max 10 annotations per task")

    if is_size_breach:
        current_size = payload.current_size
        file_path = payload.file_path
        change_summary = payload.change_summary
        assert current_size is not None
        assert file_path is not None
        assert change_summary is not None

        verdict = await asyncio.to_thread(
            _classify_breach_policy,
            file_path.strip(),
            current_size,
            payload.section,
            override_chronic=payload.force_chronic_override,
        )
        if verdict["is_chronic"] and not payload.force_chronic_override:
            content = (
                f"Size breach (chronic offender): {file_path} grew to {current_size} lines. "
                f"{verdict['reason']} Change: {change_summary.strip()}"
            )
            ctx = {
                "annotation_type": "size_breach",
                "task_type": token.get("task_type", ""),
                "file_path": file_path.strip(),
                "current_size": current_size,
                "is_chronic": True,
                "bump_count": verdict["bump_count"],
                "change_summary": change_summary.strip(),
            }
        else:
            new_limit = verdict["suggested_new_limit"]
            assert new_limit is not None
            content = (
                f"Size breach: {file_path} grew to {current_size} lines. "
                f"Suggested new limit: {new_limit}. Change: {change_summary.strip()}"
            )
            ctx = {
                "annotation_type": "size_breach",
                "task_type": token.get("task_type", ""),
                "file_path": file_path.strip(),
                "current_size": current_size,
                "new_limit": new_limit,
                "change_summary": change_summary.strip(),
            }
    else:
        content = payload.content.strip()
        ctx = {
            "annotation_type": payload.annotation_type,
            "task_type": token.get("task_type", ""),
            **({"target_page": payload.target_page} if payload.target_page else {}),
            **({"severity": payload.severity} if payload.severity else {}),
        }

    # Reproduce-on-HEAD citation grounding: an ``issue`` annotation maps to a
    # BUG_REPORT. Validate the file/symbol citations in its free-text content
    # (and any structured ``file_path``) against the project's real integration
    # branch and stash an ``_provenance_warnings`` signal when a citation is
    # unambiguously absent, so a stale issue is discounted rather than trusted.
    # Best-effort and conservative — indeterminate never flags, never fails.
    msg_type = _ANNOTATION_TYPE_MAP[payload.annotation_type]
    if msg_type in (MessageType.BUG_REPORT, MessageType.BLOCKER):
        project = await session.get(Project, task.project_id)
        if project is not None:
            structured = (
                {"file_path": payload.file_path.strip()}
                if payload.file_path and payload.file_path.strip()
                else None
            )
            warning = await _compute_message_citation_warning(
                session, project, content, structured_context=structured
            )
            if warning:
                ctx = {**ctx, "_provenance_warnings": warning}

    ctx = stamp_message_audience(
        ctx, "agent_annotation.size_breach" if is_size_breach else "agent_annotation"
    )
    msg = await build_project_message(
        session,
        project_id=task.project_id,
        message_type=msg_type,
        from_type="agent",
        from_task_id=task.id,
        content=content,
        context=ctx,
    )
    session.add(msg)
    await session.commit()

    logger.info(
        f"[ANNOTATE] {payload.annotation_type} from task {task.id} ({token.get('task_type', '?')})"
    )
    response: dict[str, Any] = {"status": "ok", "annotation_id": str(msg.id)}
    if is_size_breach:
        if "new_limit" in ctx:
            response["new_limit"] = ctx["new_limit"]
        if ctx.get("is_chronic"):
            response["is_chronic"] = True
            response["bump_count"] = ctx["bump_count"]
    return response


async def _enforce_resident_mutation_fence(
    session: AsyncSession,
    project: Project,
    task: Task,
    op: str,
) -> None:
    """Require fresh continuation and sole Strategic-lane ownership for mutations."""
    if op in ("create_message", "resolve_message"):
        return
    resident_blob = _read_resident_session(project.vision)
    operator_user_id = str(
        (resident_blob or {}).get("operator_user_id") or ""
    ).strip()
    continuation = strategic_local_continuation_status_from_vision(
        project.vision,
        operator_user_id=operator_user_id or None,
    )
    if (
        not operator_user_id
        or not continuation.get("enabled")
        or not continuation.get("fresh")
        or not continuation.get("owned_by_current_operator")
    ):
        raise HTTPException(
            status_code=409,
            detail="Resident continuation expired or ownership changed",
        )

    active_candidates = (
        await session.execute(
            select(Task).where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_(
                    (
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                    )
                ),
            )
        )
    ).scalars()
    if any(
        (candidate.first_payload or {}).get("type") in STRATEGIC_TASK_TYPES
        for candidate in active_candidates
    ):
        raise HTTPException(
            status_code=409,
            detail="Resident no longer owns the Strategic mutation lane",
        )



class StrategicOp(BaseModel):
    """Incremental strategic review operation from an agent."""

    task_id: uuid.UUID
    # The accepted op vocabulary is the canonical registry mirrored in
    # app/strategic_ops_manifest.json (single source of truth for the ./strategic
    # CLI and this dispatch chain). test_strategic_ops_registry.py
    # asserts the dispatch below accepts EXACTLY the manifest's op names.
    op: str
    data: Any = {}


@router.post("/api/agent/strategic")
@limiter.limit("60/minute")
async def strategic_operation(
    request: Request,
    payload: StrategicOp,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent endpoint for incremental strategic review operations."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
    # Message ops (create/resolve) are available to all agent types.
    # Direction/vision ops are restricted to strategic task types.
    MESSAGE_OPS = ("create_message", "resolve_message")
    if (
        payload.op not in MESSAGE_OPS
        and token.get("task_type") not in STRATEGIC_TASK_TYPES
    ):
        raise HTTPException(
            status_code=403,
            detail="Only strategic agents can modify directions/vision",
        )

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Lock project for atomic update (skip_locked + 429 avoids lock_timeout cascade)
    project = await _lock_project_or_429(session, task.project_id)

    # Message mutations follow Project -> sequence -> Task. The Project lock is
    # the strongest lock this operation needs, so no later lock upgrade can
    # occur while the shared sequence counter is held.
    preallocated_message_sequence: int | None = None
    if payload.op in MESSAGE_OPS:
        preallocated_message_sequence = (
            await lock_project_and_next_message_sequence(
                session,
                task.project_id,
                lock_timeout_ms=1_500,
            )
        )

    # Re-acquire the task row FRESH under FOR UPDATE now that we hold the project
    # lock (canonical Project→Task order — see task_locking.lock_project_then_task).
    # ``task`` was loaded above WITHOUT a lock and BEFORE the project lock, so its
    # in-memory ``context`` can be stale relative to a concurrent whole-column
    # writer. Every ``task.context = {**ctx, ...}`` read-modify-write in this
    # endpoint would otherwise overwrite the whole column from that stale read and
    # drop a concurrent writer's keys (lost update), and vice versa. Re-reading FOR UPDATE with
    # populate_existing refreshes the SHARED Task object with the current
    # committed context and holds the row until this request commits, so all
    # downstream context RMWs are serialized and race-free. Since we already hold
    # the project lock, no Project→Task writer can be holding this task lock; a
    # None here means a non-project task-locker (dispatch/reaper) holds it —
    # surface a retryable 429 (matching the project 429 contract) rather than
    # block on the 30s lock_timeout and risk a Cloud Run container-fault.
    async with task_lock(session, task.id, skip_locked=True) as _locked_task:
        if _locked_task is None:
            raise HTTPException(
                status_code=429,
                detail="Task row locked by concurrent write, retry",
                headers={"Retry-After": "2"},
            )
        task = _locked_task

    # A stopped or replaced Resident's still-valid JWT must not be able to
    # commit one last reply. Validate task/session/generation from the fresh,
    # locked Project + Task rows before staging any mutation.
    resident_ctx = task.context or {}
    if resident_ctx.get("_resident_session") is True:
        resident_blob = _read_resident_session(project.vision)
        try:
            task_generation = int(
                resident_ctx.get("_resident_worker_generation") or 1
            )
            current_generation = int(
                (resident_blob or {}).get("worker_generation") or 1
            )
        except (TypeError, ValueError):
            task_generation = current_generation = -1
        if (
            not _resident_session_is_active(resident_blob)
            or bool((resident_blob or {}).get("stop_requested"))
            or str((resident_blob or {}).get("session_id"))
            != str(resident_ctx.get("strategic_session_id"))
            or str((resident_blob or {}).get("task_id")) != str(task.id)
            or current_generation != task_generation
            or task.status in TASK_OUTCOME_TERMINAL_STATUSES
        ):
            raise HTTPException(
                status_code=409,
                detail="Resident worker generation superseded",
            )

        await _enforce_resident_mutation_fence(session, project, task, payload.op)
        # A Live Conversation lease is a server-owned public reply owner.  This
        # runs while the Project row is locked, after the Resident generation
        # validation and before *any* create_message bookkeeping, so a native
        # Workday reply that began before call activation cannot commit after it.
        # Do not delegate this to prompts: an old runner/token must fail closed.
        if payload.op == "create_message":
            from app.strategic_resident_session import (
                read_resident_call,
                resident_call_is_active,
            )

            active_call = read_resident_call(project.vision)
            if resident_call_is_active(active_call):
                raise HTTPException(
                    status_code=409,
                    detail="Call lease owns public replies for this Resident generation",
                )
    data = payload.data
    warnings: List[str] = []

    # Size guard — prevent agents from bloating project JSONB columns
    if len(json.dumps(data, default=str)) > 100_000:
        raise HTTPException(
            status_code=400, detail="Data payload too large (max 100KB)"
        )

    # Normalize the few historically surprising envelopes before their domain
    # handlers run.  Keeping this at the API boundary makes field errors
    # consistent for CLI, direct API, and future non-shell clients.
    if payload.op == "update_direction":
        data = normalize_update_direction(data)

    problem_model_dry_run = False
    if payload.op in {
        "set_problem_model",
        "patch_problem_model",
        "replace_problem_model",
    }:
        current_pm = project.problem_model if isinstance(project.problem_model, dict) else {}
        current_revision_raw = current_pm.get("revision", 0)
        current_revision = (
            current_revision_raw
            if isinstance(current_revision_raw, int) and not isinstance(current_revision_raw, bool)
            else 0
        )
        data, problem_model_dry_run, compatibility_warning = normalize_problem_model_envelope(
            payload.op,
            data,
            current_revision=current_revision,
            current_model=current_pm,
        )
        if compatibility_warning:
            warnings.append(compatibility_warning)

    def _resp(body: dict) -> dict:
        if warnings:
            body["warnings"] = list(warnings)
        return body

    async def _emit_updated(
        kind: str,
        *,
        changed_direction_ids: list[str] | None = None,
        changed_count: int | None = None,
        problem_model_changed: bool | None = None,
        running_update_changed: bool | None = None,
    ) -> None:
        """Post-commit refetch signal for a strategist output change.

        Must be awaited only AFTER the op's ``session.commit()``; the lock is
        already released by then, mirroring the established broadcast pattern.
        """
        cycle_id = task.strategic_cycle_id or (task.context or {}).get(
            "strategic_cycle_id"
        )
        await emit_strategic_updated(
            project.id,
            kind,
            task_id=task.id,
            cycle_id=cycle_id,
            output_kind=kind,
            op=payload.op,
            changed_direction_ids=changed_direction_ids,
            changed_count=changed_count,
            problem_model_changed=problem_model_changed,
            running_update_changed=running_update_changed,
        )

    async def _challenge_gate(
        op: str,
        *,
        direction_id: str | None = None,
        text: str | None = None,
    ) -> dict | None:
        """Consult the stored strategic-judgment challenge for this portfolio op.

        Hard-blocked portfolio mutations only proceed when the stored challenge
        is explicitly ok and contains a matching verdict; absent challenge
        context still behaves like the pre-gate legacy path.
        """
        from app.strategic_completion import (
            STRATEGIC_CHALLENGE_CONTEXT_KEY,
            challenge_rejects_portfolio_op,
            portfolio_op_challenge_verdict,
        )

        record = (task.context or {}).get(STRATEGIC_CHALLENGE_CONTEXT_KEY)
        blocked, why = challenge_rejects_portfolio_op(
            record, op, direction_id=direction_id, text=text
        )
        if blocked:
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            reason = f"Strategic challenge rejected this {op}"
            if why:
                reason = f"{reason}: {why}"
            logger.info(
                "[STRATEGIC API] Challenge HARD-BLOCKED %s (direction=%s) for "
                "project %s",
                op,
                direction_id,
                project.id,
            )
            body: dict = {"status": "blocked", "op": op, "reason": reason}
            if direction_id:
                body["direction_id"] = direction_id
            return _resp(body)
        item = portfolio_op_challenge_verdict(
            record, op, direction_id=direction_id, text=text
        )
        if item and str(item.get("verdict", "")).strip().lower() in (
            "warned",
            "uncertain",
        ):
            note = str(item.get("rationale", "")).strip()
            advisory = f"Strategic challenge flagged this {op} as advisory"
            if note:
                advisory = f"{advisory}: {note}"
            warnings.append(advisory)
        return None

    if payload.op == "set_vision":
        if not isinstance(data, dict) or not data.get("purpose"):
            raise HTTPException(
                status_code=400, detail="Vision must have a 'purpose' field"
            )
        from app import strategic_cycle as _strategic_cycle
        from app.vision_merge import append_rejected_edits, merge_vision

        current = dict(project.vision or {})
        # Operator-wins reconciliation, mirroring set_problem_model /
        # set_running_update: resolve the cycle's vision merge ancestor and run
        # the shared 3-way merge so a concurrent operator edit (purpose, systems,
        # priorities, context) is preserved (operator wins) and the agent's
        # superseded value is recorded to the rejected-edits ledger for next
        # cycle. On the FIRST write the ancestor is the cycle-start
        # baseline_vision snapshot; on a SECOND same-cycle write it is the
        # agent's own prior proposal (stored below) so the agent advances its
        # own field instead of being misread as an operator edit. Degrades to a
        # blind assign onto ``current`` when no cycle/baseline resolves (legacy
        # task), preserving today's behavior.
        vision_cycle_row = None
        vision_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
            "strategic_cycle_id"
        )
        merge_ancestor: dict | None = None
        if vision_cycle_id:
            vision_cycle_row = await _strategic_cycle.get(session, vision_cycle_id)
            if vision_cycle_row is not None:
                prior = _strategic_cycle.agent_vision_proposal(vision_cycle_row)
                if prior is not None:
                    merge_ancestor = prior
                else:
                    snap = _strategic_cycle.baseline_vision(vision_cycle_row)
                    if snap:
                        merge_ancestor = snap
        if merge_ancestor is not None:
            merged, rejected_edits = merge_vision(
                baseline=merge_ancestor, agent=dict(data), current=current
            )
            if rejected_edits:
                append_rejected_edits(merged, rejected_edits)
                logger.info(
                    f"[STRATEGIC API] Vision merge rejected {len(rejected_edits)} "
                    f"agent edit(s) in favor of concurrent operator changes for "
                    f"project {project.id}"
                )
            new_vision = merged
        else:
            new_vision = dict(data)
        new_vision["revision"] = (current.get("revision", 0) or 0) + 1
        new_vision["last_updated"] = _utcnow().isoformat()
        # Preserve internal tracking fields; reset failure counters on success
        new_vision.update(
            {
                k: v
                for k, v in current.items()
                if k.startswith("_") and k not in new_vision
            }
        )
        new_vision.pop("_strategic_failures", None)
        new_vision.pop("_strategic_failures_at", None)
        project.vision = new_vision
        flag_modified(project, "vision")
        session.add(project)
        # Remember the agent's pre-merge proposed vision so a subsequent
        # same-cycle write reconciles against it (see ancestor resolution above).
        if vision_cycle_row is not None:
            _strategic_cycle.set_agent_vision_proposal(vision_cycle_row, dict(data))
            session.add(vision_cycle_row)
        # Stamp task so the completion handler knows a real vision update occurred
        task.context = {**(task.context or {}), "_strategic_vision_set": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Vision updated for project {project.id} (rev {new_vision['revision']})"
        )
        return _resp(
            {"status": "ok", "op": "set_vision", "revision": new_vision["revision"]}
        )

    elif payload.op == "create_direction":
        raw_text = str(data.get("text", "")).strip()
        if not raw_text:
            raise HTTPException(status_code=400, detail="Direction text is required")
        text = _cap_str(raw_text, STRATEGIC_DIRECTION_TEXT_MAX, "text", warnings)
        raw_priority = data.get("priority", "medium")
        priority = (
            raw_priority if raw_priority in ("high", "medium", "low") else "medium"
        )
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        raw_dedup_reasoning = data.get("dedup_reasoning")
        directions = list(project.directions or [])
        # Advisory dedup (see strategic_completion._is_direction_duplicate): the
        # strategic agent owns its direction set, so a proposal that resembles an
        # existing direction is surfaced (log + a note on the created direction),
        # not hard-blocked. Hard dedup is retained only on the automated/loop-prone
        # creators. The old 409 "semantic_duplicate" rejection path is gone.
        matched = _find_matching_direction(text, directions)
        if matched:
            logger.info(
                "[STRATEGIC] API path: direction '%s' resembles existing '%s' (status=%s) — "
                "creating anyway (advisory; agent-owned set); project=%s",
                text[:60],
                (matched.get("text") or "")[:60],
                matched.get("status"),
                project.id,
            )
        retired_memory = list((project.vision or {}).get("_recently_retired_directions") or [])
        retired_grounding: dict[str, Any] | None = None
        _ret_match_raw = find_gap_respawn_match(
            text,
            str(data.get("gap_id") or "") or None,
            retired_memory,
            cooldown_days=90,
        )
        if _ret_match_raw is not None:
            retired_match, retired_match_kind = _ret_match_raw
            retirement_memo = None
            retired_direction_id = str(retired_match.get("id") or "")
            if retired_direction_id:
                try:
                    retirement_memo = await fetch_retirement_memo(
                        session,
                        project.id,
                        retired_direction_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[STRATEGIC] API path: failed to fetch retirement memo for recently-retired "
                        "direction %s on project %s",
                        retired_direction_id,
                        project.id,
                        exc_info=True,
                    )
            retired_grounding = build_retired_direction_grounding(
                retired_match,
                retirement_memo,
            )
            logger.info(
                "[STRATEGIC] API path: direction '%s' matches recently-retired '%s' "
                "(prior_outcome=%s, match=%s, memo=%s) — creating anyway (advisory; agent-owned set); "
                "project=%s",
                text[:60],
                (retired_match.get("text") or retired_direction_id)[:60],
                retired_grounding.get("classification"),
                retired_match_kind,
                (
                    retired_grounding.get("retirement_memo", {}).get("knowledge_doc_id")
                    if isinstance(retired_grounding.get("retirement_memo"), dict)
                    else "none"
                ),
                project.id,
            )
        success_evidence = str(data.get("success_evidence", "")).strip()
        retirement_condition = str(data.get("retirement_condition", "")).strip()
        if not success_evidence or not retirement_condition:
            raise HTTPException(
                status_code=400,
                detail="success_evidence and retirement_condition are required for direction creation",
            )
        _blk = await _challenge_gate("create_direction", text=text)
        if _blk is not None:
            return _blk
        # Atomic create-staged: when ``staged`` is set, the direction is born in
        # the "staged" state stamped to the CURRENT open version — identical
        # resolution semantics to ``stage_direction`` — so a next-version idea
        # never passes through a research-eligible active state (closing the old
        # create-then-stage race). Resolve the version FIRST so a missing open
        # version skips cleanly (409) without creating anything.
        staged_create = bool(data.get("staged"))
        staged_for_version: int | None = None
        if staged_create:
            from app.release_version import current_open_version

            open_version = await current_open_version(session, project.id)
            if open_version is None:
                # No open version to gate against — staging would have no target
                # to auto-activate at, so skip (409) rather than silently
                # creating an active direction the strategist meant for next
                # version (mirrors stage_direction).
                ctx = task.context or {}
                task.context = {**ctx, "_strategic_api_called": True}
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
                return JSONResponse(
                    content=_resp(
                        {
                            "status": "skipped",
                            "reason": "No open version to stage against",
                        }
                    ),
                    status_code=409,
                )
            staged_for_version = open_version.version_number
        # The active-direction cap counts active directions only; a staged
        # create is inert until its target version promotes, so it does not
        # consume a slot (mirrors the user PATCH / add_direction staged paths).
        if not staged_create:
            active_count = sum(1 for d in directions if d.get("status") == "active")
            max_active = _runtime_config.get("max_active_directions", 10)
            if active_count >= max_active:
                ctx = task.context or {}
                task.context = {**ctx, "_strategic_api_called": True}
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
                return JSONResponse(
                    content=_resp(
                        {
                            "status": "skipped",
                            "reason": f"Direction cap reached: {active_count}/{max_active} active",
                        }
                    ),
                    status_code=409,
                )
        _DIR_FIELD_MAX = 500
        criterion_keys_supplied = "criterion_keys" in data
        criterion_keys = (
            await _validate_criterion_keys_payload(
                session, project.id, data.get("criterion_keys"), "criterion_keys"
            )
            if criterion_keys_supplied
            else []
        )
        create_criterion_release_version_id: uuid.UUID | None = None
        if criterion_keys:
            from app.release_version import current_open_version as _criterion_open_version

            _criterion_version = await _criterion_open_version(session, project.id)
            create_criterion_release_version_id = (
                _criterion_version.id if _criterion_version is not None else None
            )
        new_dir: dict = {
            "id": f"d-{uuid.uuid4().hex[:8]}",
            "text": text,
            "status": "staged" if staged_create else "active",
            "priority": priority,
            "notes": (
                [
                    {
                        "text": f"Created by strategic review: {rationale}",
                        "added_at": _utcnow().isoformat(),
                    }
                ]
                if rationale
                else []
            )
            + (
                [
                    {
                        "text": (
                            f"Staged to begin after v{staged_for_version} promotes"
                        ),
                        "added_at": _utcnow().isoformat(),
                    }
                ]
                if staged_create
                else []
            )
            + (
                [
                    {
                        "text": (
                            "Advisory: resembles existing direction "
                            f"'{(matched.get('text') or '')[:80]}' "
                            f"(status={matched.get('status')}) — created on the "
                            "agent's judgment; retire one if redundant."
                        ),
                        "added_at": _utcnow().isoformat(),
                    }
                ]
                if matched
                else []
            ),
            "research_history": [],
            "context": {
                "success_evidence": success_evidence[:_DIR_FIELD_MAX],
                "retirement_condition": retirement_condition[:_DIR_FIELD_MAX],
            },
        }
        if criterion_keys_supplied:
            new_dir["criterion_keys"] = criterion_keys
            if create_criterion_release_version_id is not None:
                new_dir["criterion_release_version_id"] = str(
                    create_criterion_release_version_id
                )
        if staged_create:
            new_dir["staged_for_version"] = staged_for_version
            new_dir["staged_at"] = _utcnow().isoformat()
        impl_constraints = str(data.get("implementation_constraints", "")).strip()
        if impl_constraints:
            new_dir["context"]["implementation_constraints"] = impl_constraints[
                :_DIR_FIELD_MAX
            ]
        if isinstance(raw_dedup_reasoning, dict):
            dr: dict = {}
            closest = raw_dedup_reasoning.get("closest_directions")
            if isinstance(closest, list):
                dr["closest_directions"] = closest[:3]
            distinction = str(raw_dedup_reasoning.get("distinction") or "").strip()
            if distinction:
                dr["distinction"] = distinction[:_DIR_FIELD_MAX]
            if dr:
                new_dir["context"]["dedup_reasoning"] = dr
                new_dir["context"]["grounding_verdict"] = validate_create_direction_grounding(
                    dr,
                    directions,
                    (project.vision or {}).get("_recently_retired_directions") or [],
                )
        if data.get("system_id"):
            new_dir["system_id"] = _cap_str(
                data["system_id"], STRATEGIC_SYSTEM_ID_MAX, "system_id", warnings
            )
        # Persist the direction→gap linkage so reconciliation can map an active
        # direction back to the problem-model gap it works on. Optional and
        # string-cast; additive on the JSONB direction dict (no migration).
        if data.get("gap_id"):
            _gid = str(data["gap_id"])
            from app.problem_model import check_gap_id_write
            _gap_err, _open_ids = check_gap_id_write(_gid, project.problem_model)
            if _gap_err:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "gap_link_invalid", "reason": _gap_err, "open_gap_ids": _open_ids},
                )
            new_dir["gap_id"] = _gid
        if data.get("paths"):
            new_dir["paths"] = data["paths"]
        if data.get("category") and data["category"] in ("product", "structural"):
            new_dir["category"] = data["category"]
        if data.get("recommended_mode"):
            mode = _normalize_recommended_mode(data["recommended_mode"])
            if mode:
                new_dir["recommended_mode"] = mode
        raw_conflicts_with = data.get("conflicts_with")
        if raw_conflicts_with:
            if not isinstance(raw_conflicts_with, list) or not all(
                isinstance(c, str) for c in raw_conflicts_with
            ):
                raise HTTPException(
                    status_code=400,
                    detail="conflicts_with must be a list of direction ID strings",
                )
            existing_ids = {d.get("id") for d in directions}
            invalid = [cid for cid in raw_conflicts_with if cid not in existing_ids]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown direction IDs in conflicts_with: {invalid}",
                )
            new_dir["conflicts_with"] = raw_conflicts_with
        directions.append(new_dir)
        # Reconcile the problem-model gap this direction staffs, in the same
        # locked transaction (the project row is already held via
        # _lock_project_or_429; directions + problem_model are columns on it).
        # A fresh active direction (not a staged, not-yet-live one) staffs its
        # gap: add it to active_moves and clear any now-false deferral. Mirrors
        # the retire-side reconciliation but in the staffing direction.
        if new_dir.get("gap_id") and not staged_create:
            from app.problem_model import reconcile_gap_staffing_raw_result

            _gap_reconcile = reconcile_gap_staffing_raw_result(
                project.problem_model,
                new_dir["id"],
                new_dir["gap_id"],
                None,
                new_status=new_dir.get("status"),
                now_iso=_utcnow().isoformat(),
            )
            if _gap_reconcile.get("problem_model") is not None:
                project.problem_model = _gap_reconcile["problem_model"]
                flag_modified(project, "problem_model")
            _gap_details = _gap_reconcile.get("result") or {}
            if _gap_details.get("malformed_model") or _gap_details.get(
                "unresolved_gap_ids"
            ):
                logger.warning(
                    "[STRATEGIC API] create_direction gap staffing reconciliation "
                    "classified drift: %s",
                    _gap_details,
                )
        _captured_new_dir = new_dir
        await _with_directions_locked(
            session,
            project.id,
            lambda fresh: (
                [*fresh, _captured_new_dir]
                if not any(d.get("id") == _captured_new_dir["id"] for d in fresh)
                else fresh
            ),
        )
        ctx = task.context or {}
        created_ids = list(ctx.get("_strategic_created_ids", []))
        created_ids.append(new_dir["id"])
        new_ctx = {
            **ctx,
            "_strategic_dirs_created": ctx.get("_strategic_dirs_created", 0) + 1,
            "_strategic_created_ids": created_ids,
        }
        # An atomic staged create is also a staging decision — record it under
        # the staged counters too (parity with stage_direction) so the cycle's
        # staging activity is auditable.
        if staged_create:
            staged_ids = list(ctx.get("_strategic_staged_ids", []))
            staged_ids.append(new_dir["id"])
            new_ctx["_strategic_dirs_staged"] = ctx.get("_strategic_dirs_staged", 0) + 1
            new_ctx["_strategic_staged_ids"] = staged_ids
        task.context = new_ctx
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        if criterion_keys_supplied and criterion_keys:
            await _backfill_direction_merge_evidence_after_mapping(
                session, project.id, new_dir["id"]
            )
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[new_dir["id"]],
            changed_count=1,
        )
        logger.info(
            f"[STRATEGIC API] Created direction '{text[:60]}' "
            f"({'staged for v' + str(staged_for_version) if staged_create else 'active'}) "
            f"for project {project.id}"
        )
        resp_body: dict = {
            "status": "ok",
            "op": "create_direction",
            "direction_id": new_dir["id"],
            "grounding_verdict": new_dir.get("context", {}).get("grounding_verdict"),
        }
        if retired_grounding:
            resp_body["retired_grounding"] = retired_grounding
        if staged_create:
            resp_body["staged_for_version"] = staged_for_version
        if criterion_keys_supplied:
            resp_body["criterion_keys"] = criterion_keys
        return _resp(resp_body)

    elif payload.op == "update_direction":
        dir_id = data.get("direction_id")
        # `data` has already been normalized by normalize_update_direction:
        # canonical callers pass fields directly, while the legacy nested
        # `changes` wrapper was flattened at the boundary.
        changes = {
            key: value
            for key, value in data.items()
            if key not in {
                "direction_id",
                "rationale",
                "terminal_reason",
                "dedup_reasoning",
                "successor_id",
                "superseded_by_id",
            }
        }
        raw_dedup_reasoning = data.get("dedup_reasoning")
        if not dir_id or not changes:
            raise HTTPException(
                status_code=400, detail="direction_id and changes required"
            )
        now_iso = _utcnow().isoformat()
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        # Second hard gate: any direction update is a portfolio-shape mutation.
        # The challenger verdict is keyed by direction_id, so a rejection blocks
        # all direction-field changes, not just status flips.
        _blk = await _challenge_gate("update_direction", direction_id=str(dir_id))
        if _blk is not None:
            return _blk
        criterion_keys_supplied = "criterion_keys" in changes
        criterion_keys = (
            await _validate_criterion_keys_payload(
                session,
                project.id,
                changes.get("criterion_keys"),
                "changes.criterion_keys",
            )
            if criterion_keys_supplied
            else []
        )
        update_criterion_release_version_id: uuid.UUID | None = None
        if criterion_keys:
            from app.release_version import current_open_version as _criterion_open_version

            _criterion_version = await _criterion_open_version(session, project.id)
            update_criterion_release_version_id = (
                _criterion_version.id if _criterion_version is not None else None
            )
        directions = list(project.directions or [])
        updated = False
        # Re-ground reconciliation: capture the direction's prior gap_id BEFORE
        # any change is applied so a gap_id change can prune the old gap. Holds
        # ``(new_gap_id, prior_gap_id)`` once a gap_id change is observed.
        _gap_reground: tuple[str | None, str | None] | None = None
        _pre_status: str | None = None
        retired_memory = list((project.vision or {}).get("_recently_retired_directions") or [])
        # Set to the terminal status ("expired"/"superseded") when this update
        # flips the direction INTO a terminal state from a non-terminal one, so
        # retired-direction memory is recorded exactly once after the loop.
        _terminal_flip: str | None = None
        for d in directions:
            if d.get("id") == dir_id:
                _pre_text = d.get("text")
                _pre_se = (d.get("context") or {}).get("success_evidence")
                _pre_rc = (d.get("context") or {}).get("retirement_condition")
                _pre_gap_id = d.get("gap_id")
                _pre_status = d.get("status")
                requested_status = changes.get("status")
                if requested_status in {"expired", "superseded"} and _pre_status not in {
                    "expired",
                    "superseded",
                }:
                    from app.direction_terminal_guard import (
                        ZERO_ATTEMPT_TERMINAL_REASONS,
                        validate_direction_terminal_transition,
                    )

                    terminal_ok, terminal_error = validate_direction_terminal_transition(
                        d,
                        rationale=rationale,
                        terminal_reason=data.get("terminal_reason"),
                    )
                    if not terminal_ok:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": terminal_error,
                                "direction_id": str(dir_id),
                                "allowed_zero_attempt_terminal_reasons": sorted(
                                    ZERO_ATTEMPT_TERMINAL_REASONS
                                ),
                            },
                        )
                if "text" in changes:
                    d["text"] = _cap_str(
                        changes["text"],
                        STRATEGIC_DIRECTION_TEXT_MAX,
                        "changes.text",
                        warnings,
                    )
                if "priority" in changes and changes["priority"] in (
                    "high",
                    "medium",
                    "low",
                ):
                    d["priority"] = changes["priority"]
                if "system_id" in changes:
                    d["system_id"] = (
                        _cap_str(
                            changes["system_id"],
                            STRATEGIC_SYSTEM_ID_MAX,
                            "changes.system_id",
                            warnings,
                        )
                        if changes["system_id"]
                        else None
                    )
                if "status" in changes and changes["status"] in (
                    "active",
                    "paused",
                    "expired",
                    "superseded",
                ):
                    if changes["status"] == "active":
                        # Route reactivation through the shared seam: clears pause
                        # residue and stamps the auto_recovered_at grace marker when
                        # resuming from paused, so frozen research_history cannot
                        # immediately re-pause/zombie-retire the direction. The
                        # autonomous strategist may NOT resume a user-paused
                        # direction (allow_user_paused=False) → returns False and we
                        # emit the existing blocked response.
                        if not reactivate_paused_direction(
                            d, allow_user_paused=False
                        ):
                            ctx = task.context or {}
                            task.context = {**ctx, "_strategic_api_called": True}
                            flag_modified(task, "context")
                            session.add(task)
                            await session.commit()
                            return _resp(
                                {
                                    "status": "blocked",
                                    "reason": "Cannot reactivate user-paused direction",
                                }
                            )
                    else:
                        d["status"] = changes["status"]
                    if changes["status"] == "superseded":
                        d["completed_at"] = _utcnow().isoformat()
                        superseded_by = changes.get("superseded_by")
                        if not superseded_by:
                            succ_id = (
                                changes.get("successor_id")
                                or changes.get("superseded_by_id")
                                or data.get("successor_id")
                                or data.get("superseded_by_id")
                            )
                            if succ_id:
                                superseded_by = {
                                    "direction_id": str(succ_id),
                                    "set_at": _utcnow().isoformat(),
                                }
                        if superseded_by:
                            d["superseded_by"] = superseded_by
                    if changes["status"] in (
                        "expired",
                        "superseded",
                    ) and _pre_status not in ("expired", "superseded"):
                        _terminal_flip = changes["status"]
                        if data.get("terminal_reason"):
                            d["terminal_reason"] = (
                                str(data["terminal_reason"]).strip().lower()
                            )
                if "recommended_mode" in changes:
                    mode = _normalize_recommended_mode(changes["recommended_mode"])
                    if mode is not None:
                        d["recommended_mode"] = mode
                # Allow updating (or clearing) the direction→gap linkage so the
                # agent can re-ground an existing direction to a different gap.
                if "gap_id" in changes:
                    gid = changes["gap_id"]
                    if gid:
                        _gid = str(gid)
                        from app.problem_model import check_gap_id_write
                        _gap_err, _open_ids = check_gap_id_write(_gid, project.problem_model)
                        if _gap_err:
                            raise HTTPException(
                                status_code=400,
                                detail={"error": "gap_link_invalid", "reason": _gap_err, "open_gap_ids": _open_ids},
                            )
                        d["gap_id"] = _gid
                    else:
                        d.pop("gap_id", None)
                    _gap_reground = (d.get("gap_id"), _pre_gap_id)
                if "conflicts_with" in changes:
                    cw = changes["conflicts_with"]
                    if cw is None or (isinstance(cw, list) and len(cw) == 0):
                        d.pop("conflicts_with", None)
                    elif isinstance(cw, list) and all(isinstance(c, str) for c in cw):
                        other_ids = {
                            other.get("id")
                            for other in directions
                            if other.get("id") != dir_id
                        }
                        invalid = [cid for cid in cw if cid not in other_ids]
                        if invalid:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Unknown direction IDs in conflicts_with: {invalid}",
                            )
                        d["conflicts_with"] = cw
                    else:
                        raise HTTPException(
                            status_code=400,
                            detail="conflicts_with must be a list of direction ID strings",
                        )
                if criterion_keys_supplied:
                    d["criterion_keys"] = criterion_keys
                    if update_criterion_release_version_id is not None:
                        d["criterion_release_version_id"] = str(
                            update_criterion_release_version_id
                        )
                    else:
                        d.pop("criterion_release_version_id", None)
                # Handle success_evidence and retirement_condition in context
                _DIR_FIELD_MAX = 500
                if "success_evidence" in changes or "retirement_condition" in changes:
                    ctx = dict(d.get("context") or {})
                    old_success_evidence = ctx.get("success_evidence")
                    old_retirement_condition = ctx.get("retirement_condition")
                    if "success_evidence" in changes:
                        se = changes["success_evidence"]
                        if se is None or se == "":
                            ctx.pop("success_evidence", None)
                        else:
                            ctx["success_evidence"] = _cap_str(
                                se,
                                _DIR_FIELD_MAX,
                                "changes.success_evidence",
                                warnings,
                            )
                    if "retirement_condition" in changes:
                        rc = changes["retirement_condition"]
                        if rc is None or rc == "":
                            ctx.pop("retirement_condition", None)
                        else:
                            ctx["retirement_condition"] = _cap_str(
                                rc,
                                _DIR_FIELD_MAX,
                                "changes.retirement_condition",
                                warnings,
                            )
                    if (
                        ctx.get("success_evidence") != old_success_evidence
                        or ctx.get("retirement_condition") != old_retirement_condition
                    ):
                        _reset_scaffolding_state_on_outcome_change(d)
                    d["context"] = ctx
                if rationale:
                    notes = d.get("notes", [])
                    note_text = f"Strategic update: {rationale}"
                    if not any(n.get("text") == note_text for n in notes):
                        notes.append(
                            {
                                "text": note_text,
                                "added_at": _utcnow().isoformat(),
                            }
                        )
                        d["notes"] = _cap_list(
                            notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings
                        )
                if isinstance(raw_dedup_reasoning, dict):
                    dr: dict = {}
                    closest = raw_dedup_reasoning.get("closest_directions")
                    if isinstance(closest, list):
                        dr["closest_directions"] = closest[:3]
                    distinction = str(raw_dedup_reasoning.get("distinction") or "").strip()
                    if distinction:
                        dr["distinction"] = distinction[:_DIR_FIELD_MAX]
                    if dr:
                        ctx = dict(d.get("context") or {})
                        ctx["dedup_reasoning"] = dr
                        ctx["grounding_verdict"] = validate_create_direction_grounding(
                            dr, directions, retired_memory
                        )
                        d["context"] = ctx
                if (
                    d.get("text") != _pre_text
                    or (d.get("context") or {}).get("success_evidence") != _pre_se
                    or (d.get("context") or {}).get("retirement_condition") != _pre_rc
                ):
                    _reset_outcome_detection_state(d)
                updated = True
                break
        if not updated:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        _updated_dir = next((x for x in directions if x.get("id") == dir_id), None)
        _new_status = _updated_dir.get("status") if _updated_dir else None
        if _updated_dir is not None and (
            _gap_reground is not None or _new_status != _pre_status
        ):
            from app.problem_model import reconcile_gap_staffing_raw_result

            _new_gap_id = _updated_dir.get("gap_id")
            _prior_gap_id = (
                _gap_reground[1]
                if _gap_reground is not None
                else _updated_dir.get("gap_id")
            )
            _gap_reconcile = reconcile_gap_staffing_raw_result(
                project.problem_model,
                str(dir_id),
                _new_gap_id,
                _prior_gap_id,
                new_status=_new_status,
                prior_status=_pre_status,
                now_iso=now_iso,
            )
            if _gap_reconcile.get("problem_model") is not None:
                project.problem_model = _gap_reconcile["problem_model"]
                flag_modified(project, "problem_model")
            _gap_details = _gap_reconcile.get("result") or {}
            if _gap_details.get("malformed_model") or _gap_details.get(
                "unresolved_gap_ids"
            ):
                logger.warning(
                    "[STRATEGIC API] update_direction gap staffing reconciliation "
                    "classified drift: %s",
                    _gap_details,
                )
        # Record retired-direction memory when this update flipped the direction
        # into a terminal state, in the SAME committed transaction (mirrors the
        # retire_direction op). Only fires on a real non-terminal->terminal
        # transition, so repeated updates or already-terminal directions record
        # nothing.
        if _terminal_flip and _updated_dir is not None:
            if _terminal_flip == "superseded":
                _term_reason = "strategic_superseded"
                _sb = _updated_dir.get("superseded_by")
                _succ = (
                    _sb.get("direction_id") if isinstance(_sb, dict) else None
                )
                _term_outcome = f"superseded by {_succ}" if _succ else None
            else:
                _term_reason = "strategic_expired"
                _term_outcome = rationale or None
            _term_snapshot = dict(_updated_dir)

            def _record_terminal_memory(vision: dict) -> None:
                record_retired_direction_memory(
                    vision,
                    _term_snapshot,
                    reason=_term_reason,
                    outcome=_term_outcome,
                )

            await _with_vision_locked(session, project.id, _record_terminal_memory)
        _captured_updated_dir = dict(_updated_dir) if _updated_dir is not None else None
        _captured_update_id = str(dir_id)
        def _apply_update(fresh: list) -> list:
            if _captured_updated_dir is None:
                return fresh
            return [
                _captured_updated_dir if d.get("id") == _captured_update_id else d
                for d in fresh
            ]
        await _with_directions_locked(session, project.id, _apply_update)
        ctx = task.context or {}
        updated_ids = list(ctx.get("_strategic_updated_ids", []))
        updated_ids.append(str(dir_id))
        task.context = {
            **ctx,
            "_strategic_api_called": True,
            "_strategic_dirs_updated": ctx.get("_strategic_dirs_updated", 0) + 1,
            "_strategic_updated_ids": updated_ids,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        if criterion_keys_supplied and criterion_keys:
            await _backfill_direction_merge_evidence_after_mapping(
                session, project.id, str(dir_id)
            )
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[dir_id],
            changed_count=1,
        )
        logger.info(f"[STRATEGIC API] Updated direction {dir_id}")
        return _resp(
            {
                "status": "ok",
                "op": "update_direction",
                "direction_id": dir_id,
                "grounding_verdict": (_updated_dir.get("context") or {}).get(
                    "grounding_verdict"
                ),
                **({"criterion_keys": criterion_keys} if criterion_keys_supplied else {}),
            }
        )

    elif payload.op == "retire_direction":
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        now_iso = _utcnow().isoformat()
        ctx = task.context or {}
        directions = list(project.directions or [])
        matched_direction = None
        for d in directions:
            if d.get("id") == dir_id:
                matched_direction = d
                break
        if matched_direction is None:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        from app.direction_terminal_guard import (
            ZERO_ATTEMPT_TERMINAL_REASONS,
            validate_direction_terminal_transition,
        )

        terminal_reason = data.get("terminal_reason")
        terminal_ok, terminal_error = validate_direction_terminal_transition(
            matched_direction,
            rationale=rationale,
            terminal_reason=terminal_reason,
        )
        if not terminal_ok:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": terminal_error,
                    "direction_id": str(dir_id),
                    "allowed_zero_attempt_terminal_reasons": sorted(
                        ZERO_ATTEMPT_TERMINAL_REASONS
                    ),
                },
            )
        _blk = await _challenge_gate("retire_direction", direction_id=str(dir_id))
        if _blk is not None:
            return _blk

        matched_direction["status"] = "expired"
        if terminal_reason:
            matched_direction["terminal_reason"] = str(terminal_reason).strip().lower()
        notes = matched_direction.get("notes", [])
        note_text = f"Retired by strategic review: {rationale}"
        if not any(n.get("text") == note_text for n in notes):
            notes.append(
                {
                    "text": note_text,
                    "added_at": now_iso,
                }
            )
            matched_direction["notes"] = _cap_list(
                notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings
            )
        # Clear criterion links from the blob BEFORE the locked write so the
        # dual-write (sync_directions -> upsert_direction) atomically propagates
        # the cleared state to the Direction ORM row.  Without this, a future
        # unrelated with_directions_locked -> sync_directions re-reads stale
        # criterion_keys from the blob and resurrects them on the ORM row.
        _prior_criterion_keys = matched_direction.get("criterion_keys")
        if _prior_criterion_keys:
            matched_direction["criterion_keys"] = []
            matched_direction.pop("criterion_release_version_id", None)
            warnings.append(
                f"Retired direction {dir_id} had {len(_prior_criterion_keys)} criterion "
                f"link(s) cleared: {_prior_criterion_keys}"
            )
        _captured_retired_dir = dict(matched_direction)
        _captured_retire_id = str(dir_id)
        def _apply_retire(fresh: list) -> list:
            return [
                _captured_retired_dir if d.get("id") == _captured_retire_id else d
                for d in fresh
            ]
        await _with_directions_locked(session, project.id, _apply_retire)
        # Reconcile the problem-model gap this direction was staffing in the SAME
        # committed transaction: prune the retired id from gap.active_moves (->
        # past_moves) and, if it was the gap's last active move, record a
        # GapDeferral so the now-unstaffed gap is acknowledged instead of being
        # flagged as a stale active-move / orphaned gap next strategic cycle.
        from app.problem_model import reconcile_gaps_for_retired_directions
        from app.task_locking import with_problem_model_locked

        def _reconcile_pm(pm: dict) -> None:
            reconcile_gaps_for_retired_directions(pm, [dir_id], now_iso)

        await with_problem_model_locked(session, project.id, _reconcile_pm)
        # Record retired-direction memory in the SAME committed transaction so a
        # strategist-retired direction blocks a same-gap / text-overlap re-spawn
        # next cycle (mirrors the fallback retire path's deferred vision write).
        _retire_snapshot = dict(matched_direction)
        _retire_outcome = rationale or None

        def _record_retire_memory(vision: dict) -> None:
            record_retired_direction_memory(
                vision,
                _retire_snapshot,
                reason="strategic_retire",
                outcome=_retire_outcome,
            )

        await _with_vision_locked(session, project.id, _record_retire_memory)
        retired_ids = list(ctx.get("_strategic_retired_ids", []))
        retired_ids.append(dir_id)
        task.context = {
            **ctx,
            "_strategic_dirs_retired": ctx.get("_strategic_dirs_retired", 0) + 1,
            "_strategic_retired_ids": retired_ids,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[dir_id],
            changed_count=1,
        )
        logger.info(f"[STRATEGIC API] Retired direction {dir_id}")
        try:
            from app.completion_debt import aresolve_deferred_retirement_debt

            await aresolve_deferred_retirement_debt(session, project.id, dir_id)
            await session.commit()
        except Exception:
            logger.exception(
                "[STRATEGIC API] deferred-retirement debt resolve failed for "
                "direction %s (non-fatal)",
                dir_id,
            )
        await _spawn_retirement_memos(
            session, project, task, [dir_id], {dir_id: rationale}
        )
        await session.commit()
        # Advisory (non-blocking): each active direction is one parallel work-lane
        # the fleet fans out across, so retiring below the healthy floor caps max
        # parallelism and leaves VM capacity idle. ``directions`` already reflects
        # this retire (the row was flipped to "expired" above).
        _active_after = sum(1 for d in directions if d.get("status") == "active")
        if _active_after < MIN_HEALTHY_ACTIVE_DIRECTIONS:
            warnings.append(
                f"Only {_active_after} active direction(s) remain after this "
                f"retire. Active directions are the fleet's parallel work-lanes, "
                f"so this caps max parallelism and leaves VM capacity idle. If "
                f"there is worthwhile parallel work, consider opening a "
                f"replacement direction (aim for at least "
                f"{MIN_HEALTHY_ACTIVE_DIRECTIONS} active)."
            )
        return _resp({"status": "ok", "op": "retire_direction", "direction_id": dir_id})

    elif payload.op == "decide_churn_retirement":
        # Autonomous keep/retire decision for a direction the churn/zombie retire
        # ladder held PAUSED pending review (churn_retirement_recommended). Mirrors
        # the operator decision seam (directions.decide_churn_retirement) so the
        # flagged direction can be decided by the strategist as well as an operator.
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        decision = data.get("decision")
        if not isinstance(decision, str) or decision.upper() not in ("KEEP", "RETIRE"):
            raise HTTPException(
                status_code=400, detail="decision must be 'KEEP' or 'RETIRE'"
            )
        decision = decision.upper()
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        now_iso = _utcnow().isoformat()
        directions = list(project.directions or [])
        matched_direction = next(
            (d for d in directions if d.get("id") == dir_id), None
        )
        if matched_direction is None:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")

        if not direction_has_churn_retirement_recommendation(matched_direction):
            # Idempotent no-op — nothing pending to decide.
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return _resp(
                {
                    "status": "noop",
                    "op": "decide_churn_retirement",
                    "direction_id": dir_id,
                    "reason": "No pending churn-retirement recommendation",
                }
            )

        _did_retire = False
        if decision == "KEEP":
            # The autonomous strategist may NOT resume a user-paused direction;
            # a churn-recommended direction is auto_paused so this never blocks,
            # but keep the guard for parity with the other reactivation paths.
            if not apply_churn_retirement_keep(
                matched_direction, allow_user_paused=False
            ):
                ctx = task.context or {}
                task.context = {**ctx, "_strategic_api_called": True}
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
                return _resp(
                    {
                        "status": "blocked",
                        "reason": "Cannot reactivate user-paused direction",
                    }
                )
            result_status = "kept"
        else:
            reason = (
                "Retired by explicit strategic keep/retire decision: "
                "churn/zombie retirement recommendation confirmed."
            )
            if rationale:
                reason = f"{reason} {rationale}"
            apply_churn_retirement_retire(
                matched_direction, reason=reason, now_iso=now_iso
            )
            result_status = "retired"
            _did_retire = True

        _captured_dir = dict(matched_direction)
        _captured_id = str(dir_id)

        def _apply_decision(fresh: list) -> list:
            return [
                _captured_dir if d.get("id") == _captured_id else d for d in fresh
            ]

        await _with_directions_locked(session, project.id, _apply_decision)

        if _did_retire:
            # Reconcile the freed gap + record retired-direction memory in the same
            # committed transaction, mirroring the retire_direction op.
            from app.problem_model import reconcile_gaps_for_retired_directions
            from app.task_locking import with_problem_model_locked

            def _reconcile_pm(pm: dict) -> None:
                reconcile_gaps_for_retired_directions(pm, [dir_id], now_iso)

            await with_problem_model_locked(session, project.id, _reconcile_pm)

            _retire_snapshot = dict(matched_direction)
            _retire_outcome = rationale or None

            def _record_memory(vision: dict) -> None:
                record_retired_direction_memory(
                    vision,
                    _retire_snapshot,
                    reason="churn_retirement_decision",
                    outcome=_retire_outcome,
                )

            await _with_vision_locked(session, project.id, _record_memory)

        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[dir_id],
            changed_count=1,
        )
        logger.info(
            "[STRATEGIC API] Churn-retirement decision %s for direction %s",
            decision,
            dir_id,
        )
        return _resp(
            {
                "status": result_status,
                "op": "decide_churn_retirement",
                "direction_id": dir_id,
                "decision": decision,
            }
        )

    elif payload.op == "stage_direction":
        # Stage an EXISTING direction for the NEXT version: flip status to
        # "staged" and stamp ``staged_for_version`` to the CURRENT open
        # version_number (the version that was open when staging happened). A
        # staged direction is inert — direction_lifecycle never researches it —
        # until that version promotes, at which point
        # ``activate_eligible_staged_directions`` flips it back to "active" at
        # the version-open transition (with a reaper backstop). To stage a
        # genuinely next-version idea that does not exist yet, the strategist
        # creates the direction first (``create_direction``) and then stages it
        # by id — staging is itself the DECISION that the work belongs to the
        # next version's scope, not a way to defer a within-cycle call.
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        # Resolve the current open version to stamp the marker. Lazy import so
        # the common (non-staging) op paths don't pull in release_version.
        from app.release_version import current_open_version

        open_version = await current_open_version(session, project.id)
        directions = list(project.directions or [])
        target = next((d for d in directions if d.get("id") == dir_id), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        _blk = await _challenge_gate("stage_direction", direction_id=str(dir_id))
        if _blk is not None:
            return _blk
        if open_version is None:
            # No open version to gate against — staging would have no target to
            # auto-activate at, so surface a 409 rather than stranding the work.
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(
                content=_resp(
                    {
                        "status": "skipped",
                        "reason": "No open version to stage against",
                    }
                ),
                status_code=409,
            )
        staged_for_version = open_version.version_number
        _pre_status = target.get("status")
        _gap_id = target.get("gap_id")
        target["status"] = "staged"
        target["staged_for_version"] = staged_for_version
        target["staged_at"] = _utcnow().isoformat()
        notes = target.get("notes", [])
        notes.append(
            {
                "text": (
                    f"Staged to begin after v{staged_for_version} promotes"
                    + (f": {rationale}" if rationale else "")
                ),
                "added_at": _utcnow().isoformat(),
            }
        )
        target["notes"] = _cap_list(
            notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings
        )
        _captured_staged_dir = dict(target)
        _captured_stage_id = str(dir_id)
        def _apply_stage(fresh: list) -> list:
            return [
                _captured_staged_dir if d.get("id") == _captured_stage_id else d
                for d in fresh
            ]
        await _with_directions_locked(session, project.id, _apply_stage)
        if _gap_id:
            from app.problem_model import reconcile_gap_staffing_raw_result

            _gap_reconcile = reconcile_gap_staffing_raw_result(
                project.problem_model,
                str(dir_id),
                _gap_id,
                _gap_id,
                new_status="staged",
                prior_status=_pre_status,
                now_iso=_utcnow().isoformat(),
            )
            if _gap_reconcile.get("problem_model") is not None:
                project.problem_model = _gap_reconcile["problem_model"]
                flag_modified(project, "problem_model")
            _gap_details = _gap_reconcile.get("result") or {}
            if _gap_details.get("malformed_model") or _gap_details.get(
                "unresolved_gap_ids"
            ):
                logger.warning(
                    "[STRATEGIC API] stage_direction gap staffing reconciliation "
                    "classified drift: %s",
                    _gap_details,
                )
        ctx = task.context or {}
        staged_ids = list(ctx.get("_strategic_staged_ids", []))
        staged_ids.append(dir_id)
        task.context = {
            **ctx,
            "_strategic_dirs_staged": ctx.get("_strategic_dirs_staged", 0) + 1,
            "_strategic_staged_ids": staged_ids,
            "_strategic_api_called": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[dir_id],
            changed_count=1,
        )
        logger.info(
            f"[STRATEGIC API] Staged direction {dir_id} for v{staged_for_version} "
            f"(project {project.id})"
        )
        return _resp(
            {
                "status": "ok",
                "op": "stage_direction",
                "direction_id": dir_id,
                "staged_for_version": staged_for_version,
            }
        )

    elif payload.op == "approve_direction":
        # Approve a proposal from an anomaly detector or research agent:
        # flip status from 'proposed' to 'active'. Cap check mirrors
        # the user PATCH flow so the agent can't bust max_active_directions.
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        directions = list(project.directions or [])
        target = next((d for d in directions if d.get("id") == dir_id), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        if target.get("status") != "proposed":
            return JSONResponse(
                content=_resp(
                    {
                        "status": "skipped",
                        "reason": f"Direction is '{target.get('status')}', not 'proposed'",
                    }
                ),
                status_code=409,
            )
        _blk = await _challenge_gate("approve_direction", direction_id=str(dir_id))
        if _blk is not None:
            return _blk
        active_count = sum(1 for d in directions if d.get("status") == "active")
        max_active = _runtime_config.get("max_active_directions", 10)
        if active_count >= max_active:
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(
                content=_resp(
                    {
                        "status": "skipped",
                        "reason": f"Direction cap reached: {active_count}/{max_active} active",
                    }
                ),
                status_code=409,
            )
        target["status"] = "active"
        target["activated_at"] = _utcnow().isoformat()
        notes = target.get("notes", [])
        notes.append(
            {
                "text": f"Approved by strategic review: {rationale}"
                if rationale
                else "Approved by strategic review",
                "added_at": _utcnow().isoformat(),
            }
        )
        target["notes"] = _cap_list(
            notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings
        )
        if target.get("gap_id"):
            from app.problem_model import reconcile_gap_staffing_raw_result

            now_iso = _utcnow().isoformat()
            _gap_reconcile = reconcile_gap_staffing_raw_result(
                project.problem_model,
                dir_id,
                target["gap_id"],
                None,
                new_status="active",
                prior_status="proposed",
                now_iso=now_iso,
            )
            if _gap_reconcile.get("problem_model") is not None:
                project.problem_model = _gap_reconcile["problem_model"]
                flag_modified(project, "problem_model")
            _gap_details = _gap_reconcile.get("result") or {}
            if _gap_details.get("malformed_model") or _gap_details.get(
                "unresolved_gap_ids"
            ):
                logger.warning(
                    "[STRATEGIC API] approve_direction gap staffing reconciliation "
                    "classified drift: %s",
                    _gap_details,
                )
        _captured_approved_dir = dict(target)
        _captured_approve_id = str(dir_id)
        def _apply_approve(fresh: list) -> list:
            return [
                _captured_approved_dir if d.get("id") == _captured_approve_id else d
                for d in fresh
            ]
        await _with_directions_locked(session, project.id, _apply_approve)
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_proposals_approved": ctx.get("_strategic_proposals_approved", 0)
            + 1,
            "_strategic_dirs_updated": ctx.get("_strategic_dirs_updated", 0) + 1,
            "_strategic_updated_ids": list(ctx.get("_strategic_updated_ids", []))  # noqa: RUF005
            + [dir_id],
            "_strategic_api_called": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[dir_id],
            changed_count=1,
        )
        logger.info(
            f"[STRATEGIC API] Approved proposal {dir_id} for project {project.id}"
        )
        return _resp(
            {"status": "ok", "op": "approve_direction", "direction_id": dir_id}
        )

    elif payload.op == "reject_direction":
        # Reject a proposal — replace with a tombstone (status='rejected') so the
        # same text doesn't get re-proposed, and cancel any QUEUED tasks spawned
        # for it. Mirrors the user DELETE endpoint in routers/directions.py.
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        directions = list(project.directions or [])
        target = next((d for d in directions if d.get("id") == dir_id), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        if target.get("status") != "proposed":
            return JSONResponse(
                content=_resp(
                    {
                        "status": "skipped",
                        "reason": f"Direction is '{target.get('status')}', not 'proposed'",
                    }
                ),
                status_code=409,
            )
        _blk = await _challenge_gate("reject_direction", direction_id=str(dir_id))
        if _blk is not None:
            return _blk
        rejected_text = (target.get("text") or "")[:200].strip()
        remaining = [d for d in directions if d.get("id") != dir_id]
        if rejected_text:
            tombstone = {
                "id": target["id"],
                "text": rejected_text,
                "status": "rejected",
                "rejected_at": _utcnow().isoformat(),
                "rejected_by": "strategic",
                **({"rejection_rationale": rationale} if rationale else {}),
            }
            rejected = [d for d in remaining if d.get("status") == "rejected"]
            non_rejected = [d for d in remaining if d.get("status") != "rejected"]
            rejected.append(tombstone)
            rejected = rejected[-20:]
            remaining = non_rejected + rejected
        vm_ids_to_delete: List[str] = []
        cancelled_count = 0
        while True:
            cancelled_result = await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.QUEUED,
                    Task.context["direction_id"].as_string() == dir_id,
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
                .limit(5000)
            )
            cancelled_tasks = cancelled_result.scalars().all()
            if not cancelled_tasks:
                break
            for t in cancelled_tasks:
                t.status = TaskStatus.CANCELLED
                t.latest_status_message = (
                    f"Cancelled: proposal {dir_id} was rejected by strategic review"
                )
                session.add(t)
                if t.vm_instance_id:
                    vm_ids_to_delete.append(t.vm_instance_id)
            cancelled_count += len(cancelled_tasks)
            await session.flush()
        _captured_reject_id = str(dir_id)
        _captured_tombstone = tombstone if rejected_text else None
        def _apply_reject(fresh: list) -> list:
            _fresh_remaining = [d for d in fresh if d.get("id") != _captured_reject_id]
            if _captured_tombstone is None:
                return _fresh_remaining
            _rejected_bucket = [d for d in _fresh_remaining if d.get("status") == "rejected"]
            _non_rejected_bucket = [d for d in _fresh_remaining if d.get("status") != "rejected"]
            _rejected_bucket.append(_captured_tombstone)
            _rejected_bucket = _rejected_bucket[-20:]
            return _non_rejected_bucket + _rejected_bucket
        await _with_directions_locked(session, project.id, _apply_reject)
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_proposals_rejected": ctx.get("_strategic_proposals_rejected", 0)
            + 1,
            "_strategic_dirs_retired": ctx.get("_strategic_dirs_retired", 0) + 1,
            "_strategic_retired_ids": list(ctx.get("_strategic_retired_ids", []))  # noqa: RUF005
            + [dir_id],
            "_strategic_api_called": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        for vm_id in vm_ids_to_delete:
            # Lazy import avoids a circular dependency: main.py includes this
            # router, and _ensure_vm_deleted is defined in main.py.
            from app.vm_lifecycle import _ensure_vm_deleted as _main_ensure_vm_deleted

            await _main_ensure_vm_deleted(vm_id, reason="proposal_rejected")
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[dir_id],
            changed_count=1,
        )
        logger.info(
            f"[STRATEGIC API] Rejected proposal {dir_id} for project {project.id} (cancelled {cancelled_count} tasks)"
        )
        return _resp(
            {
                "status": "ok",
                "op": "reject_direction",
                "direction_id": dir_id,
                "cancelled_tasks": cancelled_count,
            }
        )

    elif payload.op == "activate_direction":
        # Reactivate a staged or paused direction back to active.
        # This is the sanctioned reactivation path — allows resuming user-paused
        # directions because the strategist is explicitly requesting it (unlike the
        # autonomous lifecycle which blocks user-paused via allow_user_paused=False).
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        directions = list(project.directions or [])
        target = next((d for d in directions if d.get("id") == dir_id), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        cur_status = target.get("status")
        _TERMINAL_STATUSES = frozenset(
            {"retired", "expired", "superseded", "completed", "rejected"}
        )
        if cur_status == "active":
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(
                content=_resp({"status": "skipped", "reason": "Direction is already active"}),
                status_code=409,
            )
        if cur_status in _TERMINAL_STATUSES:
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(
                content=_resp(
                    {
                        "status": "skipped",
                        "reason": f"Direction is in terminal state '{cur_status}'",
                    }
                ),
                status_code=409,
            )
        _blk = await _challenge_gate("activate_direction", direction_id=str(dir_id))
        if _blk is not None:
            return _blk
        active_count = sum(1 for d in directions if d.get("status") == "active")
        max_active = _runtime_config.get("max_active_directions", 10)
        if active_count >= max_active:
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(
                content=_resp(
                    {
                        "status": "skipped",
                        "reason": f"Direction cap reached: {active_count}/{max_active} active",
                    }
                ),
                status_code=409,
            )
        _pre_status = cur_status
        _gap_id = target.get("gap_id")
        reactivate_paused_direction(target, allow_user_paused=True)
        # reactivate_paused_direction clears staged_for_version/staged_activated_at
        # but not staged_at — pop it so staged→active leaves no staging residue.
        target.pop("staged_at", None)
        if rationale:
            notes = target.get("notes", [])
            notes.append(
                {
                    "text": f"Reactivated by strategic op: {rationale}",
                    "added_at": _utcnow().isoformat(),
                }
            )
            target["notes"] = _cap_list(
                notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings
            )
        if _gap_id:
            from app.problem_model import reconcile_gap_staffing_raw_result

            _gap_reconcile = reconcile_gap_staffing_raw_result(
                project.problem_model,
                str(dir_id),
                _gap_id,
                _gap_id,
                new_status="active",
                prior_status=_pre_status,
                now_iso=_utcnow().isoformat(),
            )
            if _gap_reconcile.get("problem_model") is not None:
                project.problem_model = _gap_reconcile["problem_model"]
                flag_modified(project, "problem_model")
            _gap_details = _gap_reconcile.get("result") or {}
            if _gap_details.get("malformed_model") or _gap_details.get(
                "unresolved_gap_ids"
            ):
                logger.warning(
                    "[STRATEGIC API] activate_direction gap staffing reconciliation "
                    "classified drift: %s",
                    _gap_details,
                )
        _captured_activated_dir = dict(target)
        _captured_activate_id = str(dir_id)
        def _apply_activate(fresh: list) -> list:
            return [
                _captured_activated_dir if d.get("id") == _captured_activate_id else d
                for d in fresh
            ]
        await _with_directions_locked(session, project.id, _apply_activate)
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_dirs_updated": ctx.get("_strategic_dirs_updated", 0) + 1,
            "_strategic_updated_ids": list(ctx.get("_strategic_updated_ids", []))  # noqa: RUF005
            + [dir_id],
            "_strategic_api_called": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(
            STRATEGIC_KIND_DIRECTIONS,
            changed_direction_ids=[dir_id],
            changed_count=1,
        )
        logger.info(
            "[STRATEGIC API] Reactivated direction %s from '%s' for project %s",
            dir_id,
            _pre_status,
            project.id,
        )
        return _resp(
            {"status": "ok", "op": "activate_direction", "direction_id": dir_id}
        )

    elif payload.op == "create_message":
        raw_content = str(data.get("content", "")).strip()
        if not raw_content:
            raise HTTPException(status_code=400, detail="Message content required")
        if len(raw_content) > STRATEGIC_MESSAGE_CONTENT_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"Message content exceeds {STRATEGIC_MESSAGE_CONTENT_MAX} character limit "
                f"({len(raw_content)} chars provided). Please resubmit with shorter content.",
            )
        content = raw_content
        try:
            msg_type = MessageType(str(data.get("type", "NOTE")).upper())
        except ValueError:
            msg_type = MessageType.NOTE
        msg_ctx = data.get("context") or {}
        if not isinstance(msg_ctx, dict):
            raise HTTPException(
                status_code=400, detail="Message context must be an object"
            )
        if (
            len(json.dumps(msg_ctx, separators=(",", ":"), default=str).encode("utf-8"))
            > STRATEGIC_MESSAGE_CONTEXT_MAX_BYTES
        ):
            raise HTTPException(
                status_code=400, detail="Message context too large (max 50KB)"
            )
        msg_ctx = dict(msg_ctx)
        # Reproduce-on-HEAD citation grounding for strategist-authored bug-class
        # messages: a strategist-emitted BUG_REPORT/BLOCKER citing code already
        # fixed/refactored away would re-enter the strategist inbox fully
        # trusted. Run the SAME conservative grounding the user/annotation paths
        # use and stash an ``_provenance_warnings`` signal when a citation is
        # unambiguously absent. Best-effort — indeterminate never flags, never
        # fails message creation. (project is already locked above.)
        if msg_type in (MessageType.BUG_REPORT, MessageType.BLOCKER):
            warning = await _compute_message_citation_warning(session, project, content)
            if warning:
                msg_ctx = {**msg_ctx, "_provenance_warnings": warning}
        # Idempotency guard: a network error can cause the caller to retry after
        # the server already committed. Accept an optional idempotency_key from
        # the agent and short-circuit if a matching row already exists.
        raw_idem = str(data.get("idempotency_key") or "").strip()
        if len(raw_idem) > 255:
            raise HTTPException(
                status_code=400,
                detail="idempotency_key exceeds 255 characters",
            )
        idem_key: str | None = raw_idem or None
        covered_context_key = "_resident_reply_covered_message_ids"
        turn_state_context_key = "_resident_reply_turn_state_sha256"
        msg_ctx.pop(covered_context_key, None)
        msg_ctx.pop(turn_state_context_key, None)
        raw_covered_ids = data.get("covered_message_ids")
        covered_message_ids: list[uuid.UUID] | None = None
        covered_message_id_strings: list[str] | None = None
        raw_conversation_claim_id = data.get("conversation_claim_id")
        conversation_claim_id: uuid.UUID | None = None
        if raw_conversation_claim_id is not None:
            try:
                conversation_claim_id = uuid.UUID(
                    str(raw_conversation_claim_id).strip()
                )
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid conversation_claim_id",
                ) from None
        raw_conversation_claim_incarnation = data.get(
            "conversation_claim_incarnation"
        )
        conversation_claim_incarnation: uuid.UUID | None = None
        if raw_conversation_claim_incarnation is not None:
            try:
                conversation_claim_incarnation = uuid.UUID(
                    str(raw_conversation_claim_incarnation).strip()
                )
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid conversation_claim_incarnation",
                ) from None
        if raw_covered_ids is not None:
            if not isinstance(raw_covered_ids, list) or not raw_covered_ids:
                raise HTTPException(
                    status_code=400,
                    detail="covered_message_ids must be a non-empty array",
                )
            if len(raw_covered_ids) > RESIDENT_REPLY_COVERED_MESSAGE_MAX:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "covered_message_ids exceeds "
                        f"{RESIDENT_REPLY_COVERED_MESSAGE_MAX} entries"
                    ),
                )
            if not idem_key:
                raise HTTPException(
                    status_code=400,
                    detail="covered_message_ids requires idempotency_key",
                )
            if resident_ctx.get("_resident_session") is not True:
                raise HTTPException(
                    status_code=409,
                    detail="covered_message_ids requires an active Resident session",
                )
            parsed_covered: list[uuid.UUID] = []
            for raw_covered_id in raw_covered_ids:
                if not isinstance(raw_covered_id, str) or not raw_covered_id.strip():
                    raise HTTPException(
                        status_code=400, detail="Invalid covered message id"
                    )
                try:
                    parsed_covered.append(uuid.UUID(raw_covered_id.strip()))
                except ValueError:
                    raise HTTPException(
                        status_code=400, detail="Invalid covered message id"
                    ) from None
            if len(set(parsed_covered)) != len(parsed_covered):
                raise HTTPException(
                    status_code=400,
                    detail="covered_message_ids contains duplicates",
                )
            covered_message_ids = parsed_covered
            covered_message_id_strings = sorted(str(value) for value in parsed_covered)
        conversation_claim_generation: int | None = None
        legacy_claim_incarnation: uuid.UUID | None = None
        if conversation_claim_id is not None:
            try:
                conversation_claim_generation = int(
                    resident_ctx.get("_resident_worker_generation") or 1
                )
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=409,
                    detail="Resident worker generation is invalid",
                ) from None
            from app.conversation_coordinator import (
                legacy_conversation_claim_incarnation,
            )

            legacy_claim_incarnation = legacy_conversation_claim_incarnation(
                task.id,
                conversation_claim_generation,
            )
            if conversation_claim_incarnation is None:
                conversation_claim_incarnation = legacy_claim_incarnation
        if (
            (
                conversation_claim_incarnation is not None
                and conversation_claim_id is None
            )
            or (
                conversation_claim_id is not None
                and covered_message_ids is None
            )
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "conversation claim incarnation requires a claim id, and "
                    "claim id requires covered message ids"
                ),
            )
        from app.routers.admin_notifications import ROUTINE_EVENT_TYPES

        event_type = str(msg_ctx.get("event_type") or "").strip() or None
        # When replying to a specific message (reply_to present) and no explicit
        # event_type was supplied, default to "question_answer" (OPERATOR_FYI)
        # instead of "strategic_message" (STRATEGIST_INTERNAL).  Without this,
        # the operator never receives a notification that a reply arrived.
        _default_event_type = (
            "question_answer"
            if event_type is None and msg_ctx.get("reply_to") is not None
            else "strategic_message"
        )
        msg_ctx = stamp_message_audience(msg_ctx, event_type or _default_event_type, message_type=msg_type.value)
        is_routine = event_type in ROUTINE_EVENT_TYPES
        if covered_message_ids is not None and is_routine:
            raise HTTPException(
                status_code=409,
                detail="Resident replies cannot cover messages from the activity surface",
            )
        if covered_message_id_strings is not None:
            msg_ctx[covered_context_key] = covered_message_id_strings
            msg_ctx["message_role"] = "operator_reply"
        if conversation_claim_id is not None:
            msg_ctx["_conversation_claim_id"] = str(conversation_claim_id)
            msg_ctx["_conversation_claim_incarnation"] = str(
                conversation_claim_incarnation
            )
        raw_turn_state = data.get("resident_turn_state")
        resident_turn_state: dict[str, Any] | None = None
        resident_turn_state_sha256: str | None = None
        if raw_turn_state is not None:
            if covered_message_ids is None:
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state requires covered_message_ids",
                )
            if not isinstance(raw_turn_state, dict):
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state must be an object",
                )
            unknown_state_keys = set(raw_turn_state) - {
                "exam_ledger",
                "decisions_reg",
                "message_cursor",
                "operator_decision_summary",
            }
            if unknown_state_keys:
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state contains unknown fields",
                )
            exam_ledger = raw_turn_state.get("exam_ledger")
            decisions_reg = raw_turn_state.get("decisions_reg")
            message_cursor = raw_turn_state.get("message_cursor")
            decision_summary = raw_turn_state.get("operator_decision_summary")
            if (
                not isinstance(exam_ledger, str)
                or not exam_ledger
                or len(exam_ledger.encode("utf-8")) > 20_000
            ):
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state.exam_ledger is invalid",
                )
            if decisions_reg is not None and (
                not isinstance(decisions_reg, str)
                or len(decisions_reg.encode("utf-8")) > 32_000
            ):
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state.decisions_reg is invalid",
                )
            if message_cursor is not None and (
                not isinstance(message_cursor, str) or len(message_cursor) > 128
            ):
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state.message_cursor is invalid",
                )
            if decision_summary is not None and not isinstance(decision_summary, dict):
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state.operator_decision_summary is invalid",
                )
            canonical_turn_state = json.dumps(
                raw_turn_state,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if len(canonical_turn_state.encode("utf-8")) > 55_000:
                raise HTTPException(
                    status_code=400,
                    detail="resident_turn_state is too large",
                )
            resident_turn_state = dict(raw_turn_state)
            resident_turn_state_sha256 = hashlib.sha256(
                canonical_turn_state.encode("utf-8")
            ).hexdigest()
            msg_ctx[turn_state_context_key] = resident_turn_state_sha256
        reply_to_message_id: uuid.UUID | None = None
        raw_reply_to = msg_ctx.get("reply_to")
        if raw_reply_to is not None:
            try:
                reply_to_message_id = uuid.UUID(str(raw_reply_to))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid reply_to message") from None
            if (
                conversation_claim_id is not None
                and (
                    covered_message_ids is None
                    or reply_to_message_id not in covered_message_ids
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="reply_to must belong to the exact conversation claim",
                )
            reply_target = (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.id == reply_to_message_id,
                        ProjectMessage.project_id == project.id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reply_target is None:
                raise HTTPException(status_code=400, detail="Invalid reply_to message")
            reply_target.handling_state = MessageHandlingState.RESPONDED
            reply_target.handled_by_task_id = task.id
            reply_target.updated_at = _utcnow()
            session.add(reply_target)
            msg_ctx["message_role"] = "operator_reply"
        # Rollback expires ORM instances even when expire_on_commit=False. Keep
        # identities as plain UUID/string values for replay paths.
        _task_id = task.id
        _project_id = project.id
        current_resident_session_id = str(
            resident_ctx.get("strategic_session_id") or ""
        )

        async def _same_message_producer(candidate: ProjectMessage) -> bool:
            if candidate.from_task_id == _task_id:
                return True
            if (
                resident_ctx.get("_resident_session") is not True
                or not current_resident_session_id
                or str(candidate.resident_session_id or "")
                != current_resident_session_id
                or candidate.from_task_id is None
            ):
                return False
            prior_task = await session.get(Task, candidate.from_task_id)
            prior_ctx = prior_task.context if prior_task is not None else {}
            return bool(
                prior_ctx
                and prior_ctx.get("_resident_session") is True
                and str(prior_ctx.get("strategic_session_id") or "")
                == current_resident_session_id
            )

        async def _apply_covered_messages() -> None:
            if covered_message_ids is None:
                return
            covered_rows = (
                (
                    await session.execute(
                        select(ProjectMessage)
                        .where(
                            ProjectMessage.project_id == _project_id,
                            ProjectMessage.id.in_(covered_message_ids),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            by_id = {row.id: row for row in covered_rows}
            if len(by_id) != len(covered_message_ids):
                raise HTTPException(
                    status_code=409,
                    detail="covered_message_ids contains an unavailable message",
                )
            if conversation_claim_id is not None:
                claim_rows = (
                    (
                        await session.execute(
                            select(ProjectMessage)
                            .where(
                                ProjectMessage.project_id == _project_id,
                                ProjectMessage.conversation_claim_id
                                == conversation_claim_id,
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if {row.id for row in claim_rows} != set(covered_message_ids):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "covered_message_ids must equal the exact "
                            "conversation claim"
                        ),
                    )
                by_id = {row.id: row for row in claim_rows}
            for covered_id in covered_message_ids:
                inbound = by_id[covered_id]
                if inbound.from_type != "user" or (
                    inbound.resident_session_id is not None
                    and str(inbound.resident_session_id)
                    != current_resident_session_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="covered_message_ids is outside this Resident session",
                    )
                if inbound.status != "open":
                    raise HTTPException(
                        status_code=409,
                        detail="covered_message_ids contains a non-open message",
                )
                if conversation_claim_id is not None:
                    if (
                        inbound.handling_state != MessageHandlingState.CLAIMED
                        or inbound.conversation_claim_id
                        != conversation_claim_id
                        or inbound.handled_by_task_id != task.id
                        or inbound.conversation_claim_generation
                        != conversation_claim_generation
                        or (
                            inbound.conversation_claim_incarnation
                            != conversation_claim_incarnation
                            and not (
                                inbound.conversation_claim_incarnation is None
                                and conversation_claim_incarnation
                                == legacy_claim_incarnation
                            )
                        )
                        or inbound.conversation_claim_expires_at is None
                        or inbound.conversation_claim_expires_at <= _utcnow()
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "conversation claim expired, superseded, "
                                "or belongs to another worker"
                            ),
                        )
                    inbound.conversation_claim_incarnation = (
                        conversation_claim_incarnation
                    )
                elif inbound.conversation_claim_id is not None:
                    # Once a message has entered the durable Coordinator path,
                    # every completion must present its exact claim. This keeps a
                    # rolling-deploy legacy worker from racing the current owner.
                    raise HTTPException(
                        status_code=409,
                        detail="conversation_claim_id is required for this message",
                    )
                inbound.status = "handled"
                inbound.handling_state = MessageHandlingState.RESPONDED
                inbound.handled_by_task_id = task.id
                inbound.conversation_claim_expires_at = None
                session.add(inbound)

        def _apply_resident_turn_state() -> None:
            if resident_turn_state is None:
                return
            next_context = dict(task.context or {})
            next_context["_exam_ledger"] = resident_turn_state["exam_ledger"]
            decisions_reg = resident_turn_state.get("decisions_reg")
            if isinstance(decisions_reg, str) and decisions_reg:
                next_context["_decisions_reg"] = decisions_reg
            message_cursor = resident_turn_state.get("message_cursor")
            if isinstance(message_cursor, str) and message_cursor:
                next_context["message_cursor"] = message_cursor
            decision_summary = resident_turn_state.get("operator_decision_summary")
            if isinstance(decision_summary, dict):
                next_context["_operator_decision_summary"] = decision_summary
            task.context = next_context
            flag_modified(task, "context")
            session.add(task)

        if idem_key:
            existing = (
                await session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.idempotency_key == idem_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if not (
                    existing.from_type == "strategic"
                    and await _same_message_producer(existing)
                    and existing.message_type == msg_type
                    and existing.content == content
                    and (existing.context or {}).get(covered_context_key)
                    == covered_message_id_strings
                    and (existing.context or {}).get("_conversation_claim_id")
                    == (
                        str(conversation_claim_id)
                        if conversation_claim_id is not None
                        else None
                    )
                    and (existing.context or {}).get(
                        "_conversation_claim_incarnation"
                    )
                    == (
                        str(conversation_claim_incarnation)
                        if conversation_claim_incarnation is not None
                        else None
                    )
                    and (existing.context or {}).get(turn_state_context_key)
                    == resident_turn_state_sha256
                    and existing.reply_to_message_id == reply_to_message_id
                    and existing.surface
                    == (
                        MessageSurface.ACTIVITY
                        if is_routine
                        else MessageSurface.CONVERSATION
                    )
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency_key already belongs to a different message",
                    )
                existing_message_id = str(existing.id)
                # Sequence allocation and reply-target bookkeeping preceded the
                # replay lookup. Exact replay is read-only.
                await session.rollback()
                return _resp(
                    {
                        "status": "ok",
                        "op": "create_message",
                        "message_id": existing_message_id,
                    }
                )
        resident_blob = _read_resident_session(project.vision)
        resident_session_id = (
            resident_blob.get("session_id")
            if isinstance(resident_blob, dict)
            and _resident_session_is_active(resident_blob)
            else None
        )
        next_sequence = preallocated_message_sequence
        if next_sequence is None:
            raise HTTPException(status_code=500, detail="Message sequence was not allocated")
        new_msg = await build_project_message(
            session,
            project_id=project.id,
            sequence=next_sequence,
            surface=(
                MessageSurface.ACTIVITY
                if is_routine
                else MessageSurface.CONVERSATION
            ),
            handling_state=(
                MessageHandlingState.PENDING
                if is_routine
                else MessageHandlingState.RESPONDED
            ),
            handled_by_task_id=_task_id if not is_routine else None,
            reply_to_message_id=reply_to_message_id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=_task_id,
            content=content,
            context=msg_ctx,
            idempotency_key=idem_key,
            resident_session_id=resident_session_id,
        )
        session.add(new_msg)
        # Reply, immutable receipt, handled inbound rows, and restart state are
        # committed together; a crash cannot split acknowledgement from reply.
        await _apply_covered_messages()
        _apply_resident_turn_state()
        # Snapshot project.id before commit: the IntegrityError rollback below
        # expires `project` (rollback expires regardless of expire_on_commit), so
        # evaluating ProjectMessage.project_id == project.id in the coalesce
        # re-query would fire a sync lazy-load -> MissingGreenlet, 500-ing the
        # idempotency-coalesce path instead of returning the existing message.
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if idem_key:
                existing = (
                    await session.execute(
                        select(ProjectMessage).where(
                            ProjectMessage.project_id == _project_id,
                            ProjectMessage.idempotency_key == idem_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if not (
                        existing.from_type == "strategic"
                        and await _same_message_producer(existing)
                        and existing.message_type == msg_type
                        and existing.content == content
                        and (existing.context or {}).get(covered_context_key)
                        == covered_message_id_strings
                        and (existing.context or {}).get("_conversation_claim_id")
                        == (
                            str(conversation_claim_id)
                            if conversation_claim_id is not None
                            else None
                        )
                        and (existing.context or {}).get(
                            "_conversation_claim_incarnation"
                        )
                        == (
                            str(conversation_claim_incarnation)
                            if conversation_claim_incarnation is not None
                            else None
                        )
                        and (existing.context or {}).get(turn_state_context_key)
                        == resident_turn_state_sha256
                        and existing.reply_to_message_id == reply_to_message_id
                        and existing.surface
                        == (
                            MessageSurface.ACTIVITY
                            if is_routine
                            else MessageSurface.CONVERSATION
                        )
                    ):
                        raise HTTPException(  # noqa: B904
                            status_code=409,
                            detail="idempotency_key already belongs to a different message",
                        )
                    # The winning transaction already committed the exact
                    # receipt. Never reapply covered-row writes after rollback.
                    existing_message_id = str(existing.id)
                    await session.rollback()
                    return _resp(
                        {
                            "status": "ok",
                            "op": "create_message",
                            "message_id": existing_message_id,
                        }
                    )
            raise
        # Broadcast so the UI can update immediately instead of waiting for poll
        await broadcast_to_project(
            str(_project_id),
            "message_created",
            message_event_payload(new_msg),
        )
        return _resp(
            {"status": "ok", "op": "create_message", "message_id": str(new_msg.id)}
        )

    elif payload.op == "resolve_message":
        if resident_ctx.get("_resident_session") is True:
            # A Resident answer must be committed through create_message with
            # covered_message_ids. Direct resolution creates only an
            # auto-generated receipt, then makes the eventual transactional
            # reply reject the now-non-open operator message. Keep this
            # invariant server-enforced so an outdated runner or prompt cannot
            # silently interrupt the conversation.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Resident sessions must stage replies with create_message; "
                    "direct resolve_message is disabled"
                ),
            )
        msg_id = data.get("message_id")
        if not msg_id:
            raise HTTPException(status_code=400, detail="message_id required")
        # Accept a full UUID (unchanged behavior) OR a short hex prefix (the
        # first 8+ hex chars the admin UI surfaces). Resolution is strictly
        # project-scoped, spans all statuses, and fails loudly on
        # ambiguous (409) / missing (404) / too-short (400) refs.
        from app.message_convergence import resolve_message_ref as _resolve_message_ref

        msg = await _resolve_message_ref(session, project.id, str(msg_id))
        was_resolved = msg.status == "resolved"
        if was_resolved and msg.message_type not in (
            MessageType.QUESTION,
            MessageType.FEEDBACK,
        ):
            return _resp({"status": "already_resolved"})
        owned_by_direction = data.get("owned_by_direction")
        owned_by_task_id = data.get("owned_by_task")
        from app.message_convergence import (
            is_bug_class as _is_bug_class,
            link_message_to_outcome as _link_message_to_outcome,
        )

        if (
            not was_resolved
            and _is_bug_class(msg)
            and (owned_by_direction or owned_by_task_id)
        ):
            # Bug-class message with ownership info: record forward link and move
            # to 'linked' state. Terminal 'resolved' happens when outcome lands.
            kind = "direction" if owned_by_direction else "task"
            identifier = owned_by_direction or owned_by_task_id
            _link_message_to_outcome(msg, kind, str(identifier))
            resolution_raw = data.get(
                "resolution", f"Fix owned by {kind}: {identifier}"
            )
            msg.resolution = _cap_str(
                resolution_raw, STRATEGIC_RESOLUTION_MAX, "resolution", warnings
            )
            session.add(msg)
            await session.commit()
            await broadcast_to_project(
                str(project.id),
                "message_updated",
                message_updated_event_payload(msg),
            )
            return _resp(
                {
                    "status": "ok",
                    "op": "resolve_message",
                    "message_id": str(msg.id),
                    "linked_to": {"kind": kind, "id": str(identifier)},
                }
            )
        if not was_resolved:
            msg.status = "resolved"
            resolution_raw = data.get("resolution", "Addressed by strategic review")
            msg.resolution = _cap_str(
                resolution_raw, STRATEGIC_RESOLUTION_MAX, "resolution", warnings
            )
            msg.resolved_at = _utcnow()
            msg.updated_at = msg.resolved_at
            msg.handling_state = MessageHandlingState.RESPONDED
            msg.handled_by_task_id = task.id
            session.add(msg)
        message_id_str = str(msg.id)
        _project_id_str = str(project.id)
        reply_msg: ProjectMessage | None = None
        reply_created = False
        if msg.message_type in (MessageType.QUESTION, MessageType.FEEDBACK):
            reply_msg = (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.reply_to_message_id == msg.id,
                    )
                    .order_by(ProjectMessage.created_at.asc(), ProjectMessage.id.asc())
                    .limit(1)
                )
            ).scalars().first()
            if reply_msg is None:
                from app.notification_dispatch import (
                    post_feedback_acknowledgment as _post_feedback_ack,
                    post_question_answer as _post_qa,
                )

                resolution = msg.resolution or "Addressed by strategic review"
                if preallocated_message_sequence is None:
                    raise HTTPException(
                        status_code=500,
                        detail="Message sequence was not allocated",
                    )
                if msg.message_type == MessageType.QUESTION:
                    reply_msg = await _post_qa(
                        session,
                        project.id,
                        msg.id,
                        resolution,
                        task.id,
                        sequence=preallocated_message_sequence,
                    )
                else:
                    reply_msg = await _post_feedback_ack(
                        session,
                        project.id,
                        msg.id,
                        resolution,
                        task.id,
                        sequence=preallocated_message_sequence,
                    )
                reply_created = True

        await session.flush()
        message_update_event = (
            message_updated_event_payload(msg) if not was_resolved else None
        )
        reply_event = (
            message_event_payload(reply_msg)
            if reply_created and reply_msg is not None
            else None
        )
        await session.commit()

        if message_update_event is not None:
            await broadcast_to_project(
                _project_id_str,
                "message_updated",
                message_update_event,
            )
        if reply_event is not None:
            await broadcast_to_project(
                _project_id_str,
                "message_created",
                reply_event,
            )
        return _resp(
            {
                "status": "already_resolved" if was_resolved else "ok",
                "op": "resolve_message",
                "message_id": message_id_str,
                **(
                    {"reply_message_id": str(reply_msg.id)}
                    if reply_msg is not None
                    else {}
                ),
            }
        )

    elif payload.op == "spawn_task" or payload.op == "spawn_fix":
        # One capability-driven task dispatch contract. spawn_fix is accepted for
        # one compatibility release but routes through this exact path.
        legacy_spawn_fix = payload.op == "spawn_fix"
        spawn_type = data.get("type")
        allowed_spawn_types = (
            STRATEGIST_SPAWNABLE_TASK_TYPES | STRATEGIST_CONTEXTUAL_TASK_TYPES
        )
        if spawn_type not in allowed_spawn_types:
            raise HTTPException(
                status_code=400,
                detail=(
                    "spawn_task type must be one of "
                    f"{sorted(allowed_spawn_types)}; got '{spawn_type}'"
                ),
            )
        raw_prompt = str(data.get("prompt", "")).strip()
        # compute_experiment is described by its launch contract (command +
        # hypothesis); a free-text prompt is optional — fall back to hypothesis.
        if not raw_prompt and spawn_type == "compute_experiment":
            raw_prompt = str(data.get("hypothesis", "")).strip()
        if not raw_prompt and spawn_type != "compute_experiment":
            raise HTTPException(status_code=400, detail="prompt is required")
        raw_title = str(data.get("title", "")).strip()
        if not raw_title:
            raise HTTPException(status_code=400, detail="title is required")
        # Cap to prevent runaway prompts consuming token budget
        prompt = _cap_str(raw_prompt, 10_000, "prompt", warnings)
        title = _cap_str(raw_title, 200, "title", warnings)
        direction_id = data.get("direction_id")
        if direction_id and not any(
            d.get("id") == direction_id for d in (project.directions or [])
        ):
            raise HTTPException(
                status_code=400, detail=f"Unknown direction_id: {direction_id}"
            )

        # Optional explicit criterion_ids — validated against active keys at
        # task-creation time (same as direction-row inheritance below). Allows
        # direction-free proving spawns to link their merge evidence to coverage
        # criteria without requiring a direction assignment.
        raw_explicit_criterion_ids = data.get("criterion_ids")
        explicit_criterion_ids: list[str] = []
        if raw_explicit_criterion_ids is not None:
            if not isinstance(raw_explicit_criterion_ids, list):
                raise HTTPException(
                    status_code=400, detail="criterion_ids must be a list of strings"
                )
            for _cid in raw_explicit_criterion_ids:
                if not isinstance(_cid, str) or not _cid.strip():
                    raise HTTPException(
                        status_code=400,
                        detail="criterion_ids entries must be non-empty strings",
                    )
            explicit_criterion_ids = [str(k).strip() for k in raw_explicit_criterion_ids[:50]]

        # Build payload — use the same validator the research/plan spawners use
        # so the new task is indistinguishable from normal autonomous spawns
        # except for the context flags.
        spawn_payload: dict = {
            "type": spawn_type,
            "prompt": prompt,
            "title": title,
            "base_branch": _get_project_integration_branch(project),
        }
        if spawn_type in STRATEGIST_CONTEXTUAL_TASK_TYPES:
            for context_field in (
                "pr_number",
                "branch",
                "original_task_id",
                "original_prompt",
            ):
                if data.get(context_field) is not None:
                    spawn_payload[context_field] = data[context_field]
        if direction_id:
            spawn_payload["direction_id"] = direction_id

        # Forward an acceptance_evidence declaration from the strategist into the
        # task payload so direction-free proving spawns can declare a named_test
        # and have their merge credited as proven acceptance evidence.
        raw_acceptance_evidence = data.get("acceptance_evidence")
        if raw_acceptance_evidence is not None:
            if isinstance(raw_acceptance_evidence, dict):
                from app.payload_schemas import AcceptanceEvidence as _AcceptanceEvidence
                try:
                    _AcceptanceEvidence.model_validate(raw_acceptance_evidence)
                    spawn_payload["acceptance_evidence"] = raw_acceptance_evidence
                except Exception:  # noqa: BLE001
                    warnings.append(
                        "acceptance_evidence declaration is malformed and was ignored"
                    )
            else:
                warnings.append(
                    "acceptance_evidence must be a dict; ignored"
                )

        # compute_experiment requires a validated launch contract with spend-gate.
        spawn_requires_approval: bool | None = None
        if spawn_type == "compute_experiment":
            from app.compute_experiment_contract import (
                _author_compute_launch_contract,
                _validate_compute_launch_contract,
                build_compute_contract_preview,
                resolve_compute_spend_decision,
            )

            contract = _author_compute_launch_contract(data)
            ok, reason = _validate_compute_launch_contract(
                contract, require_smoke=False
            )
            if not ok:
                raise HTTPException(
                    status_code=400,
                    detail=f"compute_experiment contract invalid: {reason}",
                )
            spend_cap = float(contract.get("spend_cap_usd") or 0)
            direction_ctx = next(
                (
                    d
                    for d in (project.directions or [])
                    if d.get("id") == direction_id
                ),
                None,
            )
            require_local_runner = resolve_project_require_local_runner(project)
            spend_decision = resolve_compute_spend_decision(
                runtime_config=_runtime_config,
                direction=direction_ctx,
                spend_cap_usd=spend_cap,
                require_local_runner=require_local_runner,
                task_context=dict(task.context or {}),
                launch_contract=contract,
            )
            spawn_requires_approval = spend_decision.requires_monetary_approval
            # Merge contract fields into the payload so the handler gets them.
            spawn_payload.update(contract)
            if data.get("preview_only") is True:
                return _resp(
                    {
                        "status": "ok",
                        "op": "spawn_task",
                        "preview_only": True,
                        "type": spawn_type,
                        "preview": build_compute_contract_preview(
                            contract,
                            runtime_config=_runtime_config,
                            direction=direction_ctx,
                            require_local_runner=require_local_runner,
                            task_context=dict(task.context or {}),
                        ),
                    }
                )

        # Apply per-type model/effort defaults
        type_model = _runtime_config.get("task_type_models", {}).get(spawn_type) or None
        type_effort = (
            _runtime_config.get("task_type_efforts", {}).get(spawn_type) or None
        )
        if type_model:
            spawn_payload["model"] = type_model
        if type_effort:
            spawn_payload["effort"] = type_effort
        try:
            spawn_payload = _validate_internal_payload(
                spawn_payload, f"strategic_spawn_task:{spawn_type}"
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"spawn_task cannot build a valid '{spawn_type}' payload: {exc}"
                ),
            ) from exc

        # Exact idempotency and admission are handled by StrategicSpawnRequest.
        new_task_ctx: dict = {
            "spawned_by": "strategic_direct",
            "parent_strategic_task_id": str(task.id),
        }
        if direction_id:
            new_task_ctx["direction_id"] = direction_id

        _task_criterion_release_version_id: uuid.UUID | None = None
        if direction_id or raw_explicit_criterion_ids is not None:
            from app.release_version import current_open_version as _current_open_version

            _open_version = await _current_open_version(session, project.id)
            if _open_version is not None:
                _task_criterion_release_version_id = _open_version.id
                new_task_ctx["criterion_release_version_id"] = str(
                    _open_version.id
                )

        # Resolve criterion_keys from two sources: the direction row (when
        # direction_id is set) and any explicit criterion_ids the strategist
        # passed. Both are merged and filtered to active keys on the open
        # version so stale/retracted criteria never propagate to new tasks.
        _task_criterion_ids: list[str] = []
        _keys_to_filter: list[str] = list(explicit_criterion_ids)
        if direction_id:
            from app.directions_repo import get_direction_row as _get_direction_row

            _dir_row = await _get_direction_row(session, project.id, direction_id)
            from app.acceptance_criteria import (
                direction_criterion_release_version_id as _direction_criterion_version,
            )

            _direction_version_id = _direction_criterion_version(_dir_row)
            if (
                _dir_row is not None
                and _dir_row.criterion_keys
                and (
                    _direction_version_id == _task_criterion_release_version_id
                    or _direction_version_id is None
                )
            ):
                # Merge: explicit keys first, then direction-derived; deduplicate.
                _keys_to_filter = list(
                    dict.fromkeys(_keys_to_filter + list(_dir_row.criterion_keys))
                )
        if _keys_to_filter:
            from app.acceptance_criteria import (
                filter_active_criterion_keys as _filter_active_crit,
            )
            if _task_criterion_release_version_id is not None:
                _task_criterion_ids = await _filter_active_crit(
                    session,
                    project.id,
                    _keys_to_filter,
                    release_version_id=_task_criterion_release_version_id,
                )

        # Cross-field guard: criterion_ids (explicit or direction-derived)
        # require named_test acceptance evidence so the acceptance pipeline can
        # record a proven-verdict at merge time. Without this, a criterion-carrying
        # task with observable_behavior (or absent) evidence will never close the
        # coverage loop — silently defeating the criterion-coverage gate.
        if _task_criterion_ids and (
            not isinstance(spawn_payload.get("acceptance_evidence"), dict)
            or spawn_payload["acceptance_evidence"].get("kind") != "named_test"
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "criterion_ids (explicit or direction-derived) require "
                    "acceptance_evidence with kind 'named_test' so the merge "
                    "of this task can record a proven-verdict for the "
                    "criterion(s)"
                ),
            )

        normalized_specification = {
            "debt_id": str(data.get("debt_id")) if data.get("debt_id") else None,
            "task": {
                "title": title,
                "description": prompt[:500],
                "payloads": [spawn_payload],
                "context": new_task_ctx,
                "priority": 100,
                "criterion_ids": _task_criterion_ids,
                "requires_approval": spawn_requires_approval,
                "watch": data.get("watch", True) is not False,
                "watch_purpose": str(data.get("watch_purpose") or "")[:1000],
            }
        }
        try:
            submission = await submit_strategic_spawn(
                session,
                project_id=project.id,
                parent_task_id=task.id,
                task_type=spawn_type,
                specification=normalized_specification,
                idempotency_key=data.get("idempotency_key"),
                direction_id=str(direction_id) if direction_id else None,
                burst_cap=int(
                    _runtime_config.get("strategist_spawn_window_cap", 100)
                ),
                burst_window_minutes=int(
                    _runtime_config.get("strategist_spawn_window_minutes", 60)
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        new_task = submission.task
        if new_task is None:
            return JSONResponse(
                content=_resp(
                    {
                        "status": "deferred",
                        "op": "spawn_task",
                        "request_id": str(submission.request.id),
                        "type": spawn_type,
                        "reason": submission.request.admission_reason,
                        "not_before": (
                            submission.request.not_before.isoformat()
                            if submission.request.not_before
                            else None
                        ),
                        **(
                            {"deprecated_alias": "spawn_fix"}
                            if legacy_spawn_fix
                            else {}
                        ),
                    }
                ),
                status_code=202,
            )
        logger.info(
            f"[STRATEGIC API] Accepted {spawn_type} task {new_task.id} "
            f"(priority=100) from strategic review {task.id} for project {project.id}"
        )
        return _resp(
            {
                "status": "ok",
                "op": "spawn_task",
                "request_id": str(submission.request.id),
                "task_id": str(new_task.id),
                "type": spawn_type,
                "priority": 100,
                "deduplicated": submission.replayed,
                **(
                    {"deprecated_alias": "spawn_fix"}
                    if legacy_spawn_fix
                    else {}
                ),
                **(
                    {"linked_debt_id": submission.linked_debt_id}
                    if submission.linked_debt_id
                    else {}
                ),
            }
        )

    elif (
        payload.op == "set_problem_model"
        or payload.op == "patch_problem_model"
        or payload.op == "replace_problem_model"
    ):
        # All problem-model writes funnel through the established safe merge.
        # Envelope validation above distinguishes the normal patch form, the
        # guarded recovery/bootstrapping replace form, and the legacy alias.
        # apply_agent_update still owns collection integrity, revision history,
        # and operator-wins reconciliation.
        # bumps revision, stamps last_updated, auto-archives closed/abandoned
        # gaps, preserves existing archived_gaps + revision_log, appends a
        # revision_log entry with the structured diff.
        from pydantic import ValidationError as _PmValidationError

        from app import strategic_cycle as _strategic_cycle
        from app.problem_model import (
            active_direction_gap_ids as _active_direction_gap_ids,
            apply_agent_update,
            normalize_active_moves_writeback as _normalize_active_moves_writeback,
            operator_owned_subset,
        )
        from app.research_direction_state import RETIRED_DIRECTION_STATUSES
        from app.strategic_completion import (
            NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY,
            NEW_VERSION_GOAL_CONTEXT_KEY,
            QUALIFY_REQUEST_CONTEXT_KEY,
            VERSION_PHASE_DECISION_CONTEXT_KEY,
            _apply_new_acceptance_criteria,
            _apply_new_version_goal,
            _apply_qualify_request_post_commit,
            _apply_version_phase_decision,
            extract_new_version_goal,
            extract_qualify_request,
            extract_version_phase_decision,
        )

        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="problem_model must be an object"
            )
        # version_phase_decision and qualify_request are transient per-cycle
        # signals the strategic agent embeds as top-level siblings of the
        # problem_model (see the research-plan executor prompt). Neither is part
        # of the durable model — ProblemModel is extra="forbid", so they MUST be
        # lifted out before apply_agent_update validates, or the whole update is
        # rejected (400). The sanitized dicts ride task.context to the
        # strategic-completion handler's API path; the completion handler
        # applies them (version_phase_decision flips the open ReleaseVersion's
        # phase; qualify_request kicks off the release qualifier) and neither
        # persists in project.problem_model.
        pm_data = dict(data)
        version_phase_decision = extract_version_phase_decision(
            pm_data.pop("version_phase_decision", None)
        )
        qualify_request = extract_qualify_request(pm_data.pop("qualify_request", None))
        new_version_goal = extract_new_version_goal(
            pm_data.pop("new_version_goal", None)
        )
        # new_acceptance_criteria: optional list of strings the strategist wants to
        # create as version-scoped acceptance criteria. Lifted out before
        # apply_agent_update validates (ProblemModel is extra="forbid"). The list
        # rides task.context and is applied at strategic completion.
        _raw_nac = pm_data.pop("new_acceptance_criteria", None)
        new_acceptance_criteria: list[str] | None = None
        if isinstance(_raw_nac, list):
            cleaned_nac = [str(t).strip() for t in _raw_nac if isinstance(t, str) and str(t).strip()]
            if cleaned_nac:
                new_acceptance_criteria = cleaned_nac
        # Resolve the cycle's baseline problem model (snapshot at spawn) so a
        # concurrent operator edit to an operator-owned field is merged
        # operator-wins rather than silently clobbered. Mirrors the vision
        # baseline resolution in strategic_completion; degrade to None (today's
        # blank-preservation-only behavior) when no cycle/baseline is available.
        # On the FIRST write the ancestor is the cycle-start snapshot; on a
        # SECOND same-cycle write it is the agent's own prior proposed
        # operator-owned subset (stored below after each successful write). This
        # lets a 2nd agent write advance its own context/constraints instead of
        # being misread as a mid-cycle operator edit, while a genuine operator
        # edit still moves ``current`` away from the stored proposal and wins.
        baseline_pm: dict | None = None
        pm_cycle_row = None
        pm_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
            "strategic_cycle_id"
        )
        if pm_cycle_id:
            pm_cycle_row = await _strategic_cycle.get(session, pm_cycle_id)
            if pm_cycle_row is not None:
                prior = _strategic_cycle.agent_problem_model_proposal(pm_cycle_row)
                if prior is not None:
                    baseline_pm = prior
                else:
                    snap = _strategic_cycle.baseline_problem_model(pm_cycle_row)
                    if snap:
                        baseline_pm = snap
        _protected_gap_ids = _active_direction_gap_ids(project.directions)
        _live_dir_ids = frozenset(
            str(d.get("id"))
            for d in (project.directions or [])
            if d.get("status") not in RETIRED_DIRECTION_STATUSES
        )
        try:
            new_pm_raw, pm_diff = apply_agent_update(
                current_raw=project.problem_model,
                proposed_raw=pm_data,
                now_iso=_utcnow().isoformat(),
                baseline_raw=baseline_pm,
                protected_gap_ids=_protected_gap_ids,
                live_direction_ids=_live_dir_ids,
            )
        except _PmValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Invalid problem_model: {ve}"
            ) from None
        if problem_model_dry_run:
            # The exact same validator/merge path as a write, but with no
            # project/task/context mutation or alert side effect.  This gives
            # callers an actionable preflight before a guarded replacement.
            return _resp(
                {
                    "status": "preview",
                    "op": payload.op,
                    "current_revision": current_revision,
                    "next_revision": new_pm_raw.get("revision"),
                    "diff": pm_diff,
                }
            )
        # Reject with HTTP 400 when the live core alone exceeds the byte budget.
        # Fire the alert and commit it before raising so it persists despite the 400.
        if pm_diff.get("live_core_exceeds_budget"):
            await _maybe_create_problem_model_live_core_alert(project.id, session)
            await session.commit()
            raise HTTPException(
                status_code=400,
                detail=(
                    f"problem_model live core exceeds byte budget — "
                    f"oversize section: {pm_diff['live_core_exceeds_budget']}. "
                    "Reduce the size of active gaps, live capabilities, or active friction."
                ),
            )
        project.problem_model = new_pm_raw
        flag_modified(project, "problem_model")
        # Surface blocked gap deletions (active direction still references the gap).
        _blocked_dels = pm_diff.get("gaps_deletion_blocked") or []
        for _bid in _blocked_dels:
            warnings.append(  # noqa: PERF401
                f"gap deletion blocked: {_bid} is still referenced by an active direction"
            )
        # Fire a deduplicated WARNING alert when the agent attempted a full
        # collection wipe without an explicit _deletions directive.
        for _wipe_field in ("gaps", "capabilities"):
            if pm_diff.get(f"{_wipe_field}_wipe_risk"):
                await _maybe_create_problem_model_wipe_alert(
                    project.id, session, field=_wipe_field
                )
        # Normalize stale active_moves via the shared helper so both write paths
        # (op-endpoint + log-parse fallback) stay converged.
        _active_dir_ids = {
            str(d.get("id"))
            for d in (project.directions or [])
            if d.get("status") == "active"
        }
        new_pm_raw, _norm = _normalize_active_moves_writeback(
            new_pm_raw, _active_dir_ids, _utcnow().isoformat()
        )
        if _norm:
            for _c in _norm:
                warnings.append(f"active_moves normalized: {_c}")  # noqa: PERF401
            project.problem_model = new_pm_raw
        # Carry proof records and adoption verdicts onto survivor keys when a
        # capability consolidation occurred in this write.
        _consolidated = pm_diff.get("capabilities_consolidated")
        if _consolidated:
            if not isinstance(getattr(project, "capability_proof_records", None), dict):
                project.capability_proof_records = {}
            project.capability_proof_records = carry_proof_records_for_consolidation(
                _consolidated, project.capability_proof_records
            )
            flag_modified(project, "capability_proof_records")
            if not isinstance(
                getattr(project, "capability_adoption_verdicts", None), dict
            ):
                project.capability_adoption_verdicts = {}
            project.capability_adoption_verdicts = (
                carry_adoption_verdicts_for_consolidation(
                    _consolidated, project.capability_adoption_verdicts
                )
            )
            flag_modified(project, "capability_adoption_verdicts")
        session.add(project)
        # Remember the agent's pre-merge operator-owned proposal so a subsequent
        # same-cycle write reconciles against it (see baseline resolution above).
        if pm_cycle_row is not None:
            _strategic_cycle.set_agent_problem_model_proposal(
                pm_cycle_row, operator_owned_subset(pm_data)
            )
            session.add(pm_cycle_row)
        ctx = task.context or {}
        new_ctx = {
            **ctx,
            "_strategic_api_called": True,
            "_strategic_problem_model_set": True,
        }
        # Stash keys transiently so _handle_strategic_completion can still read
        # them if something prevents the synchronous apply below from running.
        # They are popped after the synchronous apply so the completion-time
        # handler is a no-op (prevents double-application for tasks that complete
        # normally after this call).
        if version_phase_decision is not None:
            new_ctx[VERSION_PHASE_DECISION_CONTEXT_KEY] = version_phase_decision
        if qualify_request is not None:
            new_ctx[QUALIFY_REQUEST_CONTEXT_KEY] = qualify_request
        if new_version_goal is not None:
            new_ctx[NEW_VERSION_GOAL_CONTEXT_KEY] = new_version_goal
        if new_acceptance_criteria is not None:
            new_ctx[NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY] = new_acceptance_criteria
        task.context = new_ctx
        flag_modified(task, "context")
        session.add(task)
        # Apply synchronously within this handler so the decision takes immediate
        # effect rather than waiting for task completion. These apply helpers only call
        # session.add(); the single commit below persists everything atomically.
        # qualify_request is the exception: it does minutes of GitHub I/O
        # (CI-compliance scan + run_qualification) and MUST NOT run under the
        # Project FOR UPDATE lock held by this endpoint — doing so idled the
        # connection until Cloud SQL closed it, poisoning the commit. It is
        # applied post-commit on a fresh session below (see
        # _apply_qualify_request_post_commit).
        await _apply_version_phase_decision(
            version_phase_decision, task, project, session, path="api_inline"
        )
        _qr_task_id = task.id
        _qr_project_id = project.id
        await _apply_new_version_goal(
            new_version_goal, task, project, session, path="api_inline"
        )
        await _apply_new_acceptance_criteria(
            new_acceptance_criteria, task, project, session, path="api_inline"
        )
        # Pop the context keys now that they have been applied so
        # _handle_strategic_completion's completion-time apply path is a no-op
        # for each key that was handled here (idempotency guard).
        for _applied_key in (
            VERSION_PHASE_DECISION_CONTEXT_KEY,
            QUALIFY_REQUEST_CONTEXT_KEY,
            NEW_VERSION_GOAL_CONTEXT_KEY,
            NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY,
        ):
            new_ctx.pop(_applied_key, None)
        task.context = new_ctx
        flag_modified(task, "context")
        await session.commit()
        await _emit_updated(
            STRATEGIC_KIND_PROBLEM_MODEL,
            problem_model_changed=True,
        )
        # qualify_request runs AFTER the commit that released the Project lock, on
        # its own fresh session, so its slow GitHub I/O never idles this request's
        # locked connection (which would poison the commit and 500 the agent). The
        # agent still awaits it here, preserving synchronous semantics.
        await _apply_qualify_request_post_commit(
            qualify_request,
            task_id=_qr_task_id,
            project_id=_qr_project_id,
            path="api_inline",
        )
        logger.info(
            f"[STRATEGIC API] problem_model updated for project {project.id} "
            f"to revision {new_pm_raw.get('revision')} ({pm_diff or 'no diff'})"
        )
        if version_phase_decision is not None:
            logger.info(
                "[STRATEGIC API] version_phase_decision (phase=%s) applied "
                "synchronously for task %s",
                version_phase_decision["phase"],
                task.id,
            )
        if qualify_request is not None:
            logger.info(
                "[STRATEGIC API] qualify_request applied synchronously for "
                "task %s — release qualifier ran if open version is in stabilize",
                task.id,
            )
        if new_version_goal is not None:
            logger.info(
                "[STRATEGIC API] new_version_goal applied synchronously for task %s",
                task.id,
            )
        if new_acceptance_criteria is not None:
            logger.info(
                "[STRATEGIC API] new_acceptance_criteria (%d items) applied "
                "synchronously for task %s",
                len(new_acceptance_criteria),
                task.id,
            )
        return _resp(
            {
                "status": "ok",
                "op": payload.op,
                "revision": new_pm_raw.get("revision"),
                "diff": pm_diff,
                "version_phase_decision_received": version_phase_decision is not None,
                "qualify_request_received": qualify_request is not None,
                "new_version_goal_received": new_version_goal is not None,
                "new_acceptance_criteria_received": new_acceptance_criteria is not None,
            }
        )

    elif payload.op == "set_running_update":
        # Replace project.running_update with a single markdown document the
        # strategic agent maintains as a living narrative. Rewritten each
        # cycle — not appended. User acknowledgement only stamps the
        # timestamp; the document persists and the agent refines it next
        # cycle. Expects data = {"markdown": "..."}.
        doc = data.get("markdown") if isinstance(data, dict) else None
        if not isinstance(doc, str):
            raise HTTPException(
                status_code=400,
                detail="set_running_update requires data={'markdown': '<string>'}",
            )
        if len(doc) > 100_000:
            raise HTTPException(
                status_code=400,
                detail=f"Running update exceeds 100KB character limit "
                f"({len(doc)} chars provided). Please resubmit with shorter content.",
            )
        # Operator-wins reconciliation, mirroring set_problem_model: resolve the
        # cycle's baseline running_update (snapshot at spawn) and reconcile the
        # agent's document against the current DB document via the single shared
        # helper used by both this live op and the strategic-completion
        # fallback. A concurrent operator body edit is preserved (operator wins)
        # and the agent's superseded document is recorded for next cycle; a
        # mid-cycle routine-event digest append is re-integrated rather than
        # discarded. Degrades to today's blind-assign when no cycle/baseline is
        # resolvable (legacy task).
        from app import strategic_cycle as _strategic_cycle
        from app.running_update_merge import apply_reconciled_running_update

        ru_baseline: str | None = None
        ru_cycle_row = None
        ru_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
            "strategic_cycle_id"
        )
        if ru_cycle_id:
            ru_cycle_row = await _strategic_cycle.get(session, ru_cycle_id)
            if ru_cycle_row is not None:
                ru_baseline = _strategic_cycle.baseline_running_update(ru_cycle_row)
        ru_rejected = apply_reconciled_running_update(
            project, baseline=ru_baseline, agent=doc, cycle=ru_cycle_row
        )
        session.add(project)
        if ru_cycle_row is not None:
            # Persist the advanced baseline alongside the project write so the
            # next same-cycle agent write reconciles against this document.
            session.add(ru_cycle_row)
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_api_called": True,
            "_strategic_running_update_set": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(
            STRATEGIC_KIND_RUNNING_UPDATE,
            running_update_changed=True,
        )
        if ru_rejected is not None:
            logger.info(
                f"[STRATEGIC API] running_update: concurrent operator edit "
                f"preserved for project {project.id}; agent's document "
                f"({len(doc)} chars) recorded as a rejected edit"
            )
        else:
            logger.info(
                f"[STRATEGIC API] Set running_update for project {project.id} "
                f"(length: {len(doc)} chars)"
            )
        return _resp(
            {
                "status": "ok",
                "op": "set_running_update",
                "length": len(doc),
            }
        )

    elif payload.op == "reflect":
        # Append a self-reflection entry to project.self_reflection_log.
        # Drives the self-improvement loop on self-repo projects.
        from pydantic import ValidationError as _SrValidationError

        from app.problem_model import SelfReflectionEntry, append_self_reflection

        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="self_reflection entry must be an object"
            )
        try:
            entry = SelfReflectionEntry.model_validate(
                {
                    "ts": _utcnow().isoformat(),
                    "cycle_task_id": str(task.id),
                    **data,
                }
            )
        except _SrValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Invalid self_reflection entry: {ve}"
            ) from None
        project.self_reflection_log = append_self_reflection(
            project.self_reflection_log, entry
        )
        flag_modified(project, "self_reflection_log")
        session.add(project)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Appended self_reflection for project {project.id} "
            f"(log size: {len(project.self_reflection_log)})"
        )
        return _resp(
            {
                "status": "ok",
                "op": "reflect",
                "log_size": len(project.self_reflection_log),
            }
        )

    elif payload.op == "set_memory":
        # Persist a durable behavioral-memory fact. Each fact becomes ONE
        # individually-addressable, trigram-searchable KnowledgeDocument row
        # (source_kind='strategic_memory'), giving strategic memory unbounded
        # capacity, keyword retrieval (./fetch knowledge --source-kind
        # strategic_memory), and per-fact lifecycle — replacing the capped
        # strategic_memory_notes JSONB list. No challenge gate — personal
        # memory op; the fact is agent-self-attested.
        #
        # Backward-compatible op shape: the legacy `note` field is still
        # accepted (mapped to the document body); richer optional fields
        # (title/summary/topic/tags/supersedes_id) let the agent categorize and
        # supersede facts. The KnowledgeDocument row is the SOLE write path; the
        # legacy strategic_memory_notes JSONB dual-write was cut once every
        # reader switched to build_strategic_memory_index() (migration 0699
        # backfilled the pre-existing legacy notes).
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400,
                detail="set_memory requires a data object",
            )
        # Body accepts `body` (new) or `note` (legacy); at least one non-empty.
        raw_body = data.get("body")
        if not isinstance(raw_body, str) or not raw_body.strip():
            raw_body = data.get("note")
        if not isinstance(raw_body, str) or not raw_body.strip():
            raise HTTPException(
                status_code=400,
                detail="set_memory requires data.note (or data.body) as a non-empty string",
            )
        # Per-fact body cap (10 KB UTF-8) to keep any single row lean, consistent
        # with the prior note cap and set_session_summary's 10 KB limit.
        _MAX_MEMORY_NOTE_BYTES = 10_000
        body_bytes = raw_body.strip().encode("utf-8")
        if len(body_bytes) > _MAX_MEMORY_NOTE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Memory note too large ({len(body_bytes)} bytes, max {_MAX_MEMORY_NOTE_BYTES})",
            )
        fact_body = raw_body.strip()

        # Title: agent-supplied short descriptor, else derived from the body's
        # first line (≤120 chars) so every row is recognizable in the index.
        raw_title = data.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            fact_title = raw_title.strip()[:120]
        else:
            fact_title = " ".join(fact_body.split())[:120]

        # Summary: one-line distillation for the compact spawn index; falls back
        # to a truncation of the body.
        raw_summary = data.get("summary")
        if isinstance(raw_summary, str) and raw_summary.strip():
            fact_summary = " ".join(raw_summary.split())[:280]
        else:
            fact_summary = " ".join(fact_body.split())[:280]

        # Topic: constrained to a stable taxonomy so the index/browse vocabulary
        # cannot drift. Unknown topics are rejected at write time. Optional.
        _ALLOWED_MEMORY_TOPICS = {
            "operator_preference",
            "technical_constraint",
            "architecture_pattern",
            "process_rule",
            "known_failure_mode",
            "self_improvement_observation",
        }
        raw_topic = data.get("topic")
        fact_topic: str | None = None
        if raw_topic is not None:
            if not isinstance(raw_topic, str) or raw_topic.strip() not in _ALLOWED_MEMORY_TOPICS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "set_memory data.topic must be one of "
                        f"{sorted(_ALLOWED_MEMORY_TOPICS)}"
                    ),
                )
            fact_topic = raw_topic.strip()

        # Tags: optional free-form list of short string labels.
        raw_tags = data.get("tags")
        fact_tags: list[str] = []
        if isinstance(raw_tags, list):
            fact_tags = [str(t).strip()[:64] for t in raw_tags if str(t).strip()][:20]

        # supersedes_id: optionally mark an existing strategic_memory fact as
        # dismissed (superseded) in the same transaction.
        superseded_id = None
        raw_supersedes = data.get("supersedes_id")
        if raw_supersedes:
            try:
                superseded_id = uuid.UUID(str(raw_supersedes))
            except (ValueError, AttributeError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail="set_memory data.supersedes_id must be a valid document UUID",
                ) from None

        direction_id = None
        if isinstance(task.context, dict):
            direction_id = task.context.get("direction_id")

        doc = KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            source_kind=SourceKind.STRATEGIC_MEMORY.value,
            title=fact_title,
            summary=fact_summary,
            body=fact_body,
            topic=fact_topic,
            direction_id=direction_id if isinstance(direction_id, str) else None,
            tags=fact_tags,
            # Provenance survives task cleanup (FK is ON DELETE SET NULL).
            source_citations=[{"task_id": str(task.id)}],
            # Agent-self-attested: strategic memory is written directly, not
            # through a forward-quality grader task. Consumers that filter on
            # quality can include or exclude on this strategy marker.
            forward_quality_verdict={"passed": True, "strategy": "agent_self_attested"},
            # Active fact. Superseding a fact marks the OLD doc dismissed below.
            lifecycle_status="consumed",
        )
        session.add(doc)

        if superseded_id is not None:
            old = (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == superseded_id,
                        KnowledgeDocument.project_id == project.id,
                        KnowledgeDocument.source_kind == SourceKind.STRATEGIC_MEMORY.value,
                    )
                )
            ).scalar_one_or_none()
            if old is not None:
                old.lifecycle_status = "dismissed"
                old.dismissed_at = _utcnow()
                old.dismissed_reason = f"superseded by {doc.id}"
                session.add(old)

        # The KnowledgeDocument row is now the SOLE write path for strategic
        # memory. The legacy Project.strategic_memory_notes JSONB list is no
        # longer written (readers switched to build_strategic_memory_index);
        # the column is retained for rollback safety but frozen. Migration 0699
        # backfilled pre-existing notes into KnowledgeDocument rows.

        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Wrote strategic_memory KnowledgeDocument {doc.id} "
            f"for project {project.id} (topic={fact_topic})"
        )
        # entry_count now reflects the active (non-superseded) strategic_memory
        # KnowledgeDocument rows, the authoritative store, rather than the frozen
        # legacy JSONB list.
        entry_count = (
            await session.execute(
                select(_func.count())
                .select_from(KnowledgeDocument)
                .where(
                    KnowledgeDocument.project_id == project.id,
                    KnowledgeDocument.source_kind == SourceKind.STRATEGIC_MEMORY.value,
                    KnowledgeDocument.lifecycle_status == "consumed",
                )
            )
        ).scalar_one()
        return _resp(
            {
                "status": "ok",
                "op": "set_memory",
                "doc_id": str(doc.id),
                "source_kind": SourceKind.STRATEGIC_MEMORY.value,
                "superseded_id": str(superseded_id) if superseded_id is not None else None,
                "entry_count": int(entry_count),
            }
        )

    elif payload.op == "pending_confirm":
        # Add a pending_user_confirmation item to the problem_model.
        # Scoped to external blockers only: credentials, access, major
        # pivots. Agents can ONLY create — users resolve via the UI.
        from pydantic import ValidationError as _PcValidationError

        from app.problem_model import (
            PENDING_USER_CONFIRMATION_MAX,
            PendingConfirmation,
            parse_problem_model,
            sanitize_problem_model_raw,
        )

        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="pending_confirmation must be an object"
            )
        try:
            pending = PendingConfirmation.model_validate(
                {
                    "id": data.get("id") or f"p-{uuid.uuid4().hex[:8]}",
                    "created_at": _utcnow().isoformat(),
                    **data,
                }
            )
        except _PcValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Invalid pending_confirmation: {ve}"
            ) from None
        try:
            current_model = parse_problem_model(
                sanitize_problem_model_raw(project.problem_model or {})
            )
        except _PcValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Stored problem_model is malformed: {ve}"
            ) from None
        # Idempotent: same caller-supplied id on a retry is a no-op.
        if any(
            p.id == pending.id
            for p in current_model.pending_user_confirmation
        ):
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return _resp({"status": "ok", "op": "pending_confirm", "id": pending.id})
        if len(current_model.pending_user_confirmation) >= PENDING_USER_CONFIRMATION_MAX:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"pending_user_confirmation is at the cap "
                    f"({PENDING_USER_CONFIRMATION_MAX}); resolve existing items before adding more"
                ),
            )
        current_model.pending_user_confirmation.append(pending)
        from app.problem_model import append_revision_log as _append_revision_log

        now_iso = _utcnow().isoformat()
        current_model.revision = (current_model.revision or 0) + 1
        current_model.last_updated = now_iso
        # Collection-only write to the agent-owned confirmation queue: do NOT
        # restamp last_updated_by as "agent". That field is whole-model write
        # attribution, and _preserve_operator_intent reads it (== "user") to know
        # a context-only operator edit is operator-owned. Overwriting it here would
        # let a later agent proposal with blank context silently wipe the operator's
        # context. The revision bump + revision-log entry keep this change audited.
        _append_revision_log(
            current_model,
            "added pending_user_confirmation",
            {"pending_confirmation_added": [pending.id]},
        )
        from app.problem_model import enforce_problem_model_size as _enforce_pm_size

        _gc_summary, _oversize_section = _enforce_pm_size(current_model)
        if _oversize_section:
            await _maybe_create_problem_model_live_core_alert(project.id, session)
            await session.commit()
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Problem model live core exceeds byte budget after adding "
                    f"pending_confirmation: {_oversize_section}. Reduce the size "
                    f"of active state before adding new items."
                ),
            )
        project.problem_model = current_model.model_dump()
        flag_modified(project, "problem_model")
        session.add(project)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Added pending_confirmation '{pending.id}' to project {project.id}"
        )
        return _resp(
            {
                "status": "ok",
                "op": "pending_confirm",
                "id": pending.id,
            }
        )

    elif payload.op == "resolve_pending_confirm":
        # Strategist clears its OWN pending_user_confirmation item: it obtained
        # the credential/decision, or the condition became moot. First-class
        # sibling of the create-only ``pending_confirm`` op — the strategist no
        # longer has to wait for the operator to hand-dismiss a bookkeeping ask.
        # Operator dismiss/accept remains the fallback.
        from pydantic import ValidationError as _RpcVE

        from app.problem_model import parse_problem_model, sanitize_problem_model_raw

        target_id = ""
        if isinstance(data, dict):
            target_id = str(data.get("id") or "").strip()
        elif isinstance(data, str):
            target_id = data.strip()
        if not target_id:
            raise HTTPException(
                status_code=400,
                detail="resolve_pending_confirm requires an 'id' of the pending confirmation to clear",
            )
        try:
            current_model = parse_problem_model(
                sanitize_problem_model_raw(project.problem_model or {})
            )
        except _RpcVE as ve:
            raise HTTPException(
                status_code=400, detail=f"Stored problem_model is malformed: {ve}"
            ) from None
        before = list(current_model.pending_user_confirmation)
        remaining = [p for p in before if p.id != target_id]
        removed = len(before) - len(remaining)
        if removed:
            current_model.pending_user_confirmation = remaining
            from app.problem_model import (
                append_revision_log as _append_revision_log,
                enforce_problem_model_size as _enforce_pm_size,
            )

            now_iso = _utcnow().isoformat()
            current_model.revision = (current_model.revision or 0) + 1
            current_model.last_updated = now_iso
            # Collection-only write to the agent-owned confirmation queue: preserve
            # the prior last_updated_by (see the pending_confirm branch above). This
            # provenance is whole-model write attribution that _preserve_operator_intent
            # relies on to protect an operator context edit; restamping "agent" here
            # would erase it.
            _append_revision_log(
                current_model,
                "resolved pending_user_confirmation",
                {"pending_confirmation_resolved": [target_id]},
            )
            # SHRINK path: always persist the (now-gc-bounded) model.  Alert when
            # the live core alone exceeds the budget after full eviction — the
            # operator needs to know even though we do not reject.
            _gc_summary, _oversize_section = _enforce_pm_size(current_model)
            if _oversize_section:
                logger.error(
                    "[STRATEGIC API] resolve_pending_confirm: project %s "
                    "problem_model live core still over budget after gc: %s",
                    project.id,
                    _oversize_section,
                )
                await _maybe_create_problem_model_live_core_alert(project.id, session)
            project.problem_model = current_model.model_dump()
            flag_modified(project, "problem_model")
            session.add(project)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] resolve_pending_confirm '{target_id}' on project "
            f"{project.id} — removed={bool(removed)}"
        )
        return _resp(
            {
                "status": "ok",
                "op": "resolve_pending_confirm",
                "id": target_id,
                "removed": bool(removed),
            }
        )

    elif payload.op == "set_retirement_critiques":
        # Store structured critique(s) of retired directions so the completion
        # handler can persist them in StrategicCycle metadata for future cycles.
        # data must be a list of critique objects, or {"no_eligible_retirements": true}.
        if isinstance(data, dict) and data.get("no_eligible_retirements"):
            critiques: object = {"no_eligible_retirements": True}
        elif isinstance(data, list):
            cleaned = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                direction_id = str(item.get("direction_id", "")).strip()
                what_addressed = str(item.get("what_it_addressed", "")).strip()
                pattern = str(item.get("pattern_that_produced_it", "")).strip()
                lesson = str(item.get("lesson", "")).strip()
                if direction_id and what_addressed and pattern and lesson:
                    cleaned.append(
                        {
                            "direction_id": direction_id[:64],
                            "what_it_addressed": what_addressed[:500],
                            "pattern_that_produced_it": pattern[:500],
                            "lesson": lesson[:300],
                        }
                    )
            if not cleaned:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "set_retirement_critiques: list must contain at least one valid "
                        "critique object (with direction_id, what_it_addressed, "
                        'pattern_that_produced_it, and lesson). Use '
                        '{"no_eligible_retirements": true} when no retirements exist.'
                    ),
                )
            critiques = cleaned
        else:
            raise HTTPException(
                status_code=400,
                detail='set_retirement_critiques: data must be a list of critique objects or {"no_eligible_retirements": true}',
            )
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_retirement_critiques": critiques,
            "_strategic_api_called": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        n = len(critiques) if isinstance(critiques, list) else 0
        logger.info(
            f"[STRATEGIC API] Stored {n} retirement critique(s) for task {task.id}"
        )
        return _resp({"status": "ok", "op": "set_retirement_critiques", "count": n})

    elif payload.op == "set_evidence_progress":
        # OPTIONAL refinement of the deterministic outcome-progress (scaffolding)
        # detector. data is a list of {direction_id, status, rationale} items;
        # status ∈ {advancing, stalled, met}. advancing/met SUPPRESS the advisory
        # scaffolding flag, stalled reinforces it. Stashed in task.context for the
        # completion handler to persist onto directions. Idempotent (last write
        # wins). Omitting this op entirely leaves the deterministic backstop intact.
        from app.strategic_completion import (
            EVIDENCE_PROGRESS_CONTEXT_KEY,
            _EVIDENCE_PROGRESS_RATIONALE_MAX,
            _VALID_EVIDENCE_PROGRESS,
        )

        if not isinstance(data, list):
            raise HTTPException(
                status_code=400,
                detail="set_evidence_progress: data must be a list of {direction_id, status, rationale} objects",
            )
        cleaned_ep = []
        for item in data:
            if not isinstance(item, dict):
                continue
            direction_id = str(item.get("direction_id", "")).strip()
            status = str(item.get("status", "")).strip().lower()
            rationale = str(item.get("rationale", "")).strip()
            if not direction_id:
                continue
            if status not in _VALID_EVIDENCE_PROGRESS:
                raise HTTPException(
                    status_code=400,
                    detail=f"set_evidence_progress: invalid status '{status}' (must be one of: advancing, stalled, met)",
                )
            cleaned_ep.append(
                {
                    "direction_id": direction_id[:64],
                    "status": status,
                    "rationale": rationale[:_EVIDENCE_PROGRESS_RATIONALE_MAX],
                }
            )
        ctx = task.context or {}
        task.context = {
            **ctx,
            EVIDENCE_PROGRESS_CONTEXT_KEY: cleaned_ep,
            "_strategic_api_called": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Stored {len(cleaned_ep)} evidence_progress verdict(s) for task {task.id}"
        )
        return _resp(
            {"status": "ok", "op": "set_evidence_progress", "count": len(cleaned_ep)}
        )

    elif payload.op == "declare_capability_adoption":
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    "declare_capability_adoption: data must be "
                    '{"cap_name": str, "verdict": "adopt" | "reject" | "continue-trial", "rationale": str | None}'
                ),
            )
        cap_name = data.get("cap_name")
        verdict = data.get("verdict")
        rationale = data.get("rationale")
        if not isinstance(cap_name, str) or not cap_name.strip():
            raise HTTPException(
                status_code=400,
                detail="declare_capability_adoption: cap_name must be a non-empty string",
            )
        if not isinstance(verdict, str) or verdict not in (
            "adopt",
            "reject",
            "continue-trial",
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "declare_capability_adoption: verdict must be one of "
                    "'adopt', 'reject', or 'continue-trial'"
                ),
            )
        if rationale is not None and not isinstance(rationale, str):
            raise HTTPException(
                status_code=400,
                detail="declare_capability_adoption: rationale must be a string or null",
            )
        entry: dict[str, Any] = {
            "verdict": verdict,
            "evidence_source": "strategic_review",
            "evaluated_at": _utcnow().isoformat(),
        }
        rationale_text = rationale.strip() if isinstance(rationale, str) else ""
        if rationale_text:
            entry["rationale"] = rationale_text
        record_trial_verdict(project, cap_name.strip(), entry)
        await session.flush()
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_adoption_declared": ctx.get("_strategic_adoption_declared", 0)
            + 1,
            "_strategic_api_called": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            "[STRATEGIC API] Declared capability adoption for %s=%s (%s) on project %s",
            cap_name.strip(),
            verdict,
            entry["evidence_source"],
            project.id,
        )
        return _resp(
            {
                "status": "ok",
                "op": "declare_capability_adoption",
                "cap_name": cap_name.strip(),
                "verdict": verdict,
            }
        )

    elif payload.op == "cite_memos":
        # Store the memo task_ids the thinker cited in its analysis.
        # The completion handler reads this to count memos_cited reliably —
        # the previous text-scan approach always returned 0 because it scanned
        # the status message (a mechanical template) rather than the thinker prose.
        # data must be {"cited_memo_ids": [<uuid-string>, ...]}.
        if not isinstance(data, dict) or not isinstance(
            data.get("cited_memo_ids"), list
        ):
            raise HTTPException(
                status_code=400,
                detail='cite_memos: data must be {"cited_memo_ids": [...]}',
            )
        raw_ids = data["cited_memo_ids"]
        cited_ids = [
            str(i).strip()[:64]
            for i in raw_ids
            if isinstance(i, str) and str(i).strip()
        ]
        ctx = task.context or {}
        # NOTE: deliberately do NOT set _strategic_api_called here. cite_memos
        # is auto-invoked by the thinker pass whenever memos exist (before the
        # executor runs), so flagging api-usage would mask a no-op/broken
        # executor cycle as a success and suppress the failure circuit-breaker.
        task.context = {
            **ctx,
            "_strategic_cited_memo_ids": cited_ids,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Stored {len(cited_ids)} cited memo id(s) for task {task.id}"
        )
        return _resp({"status": "ok", "op": "cite_memos", "count": len(cited_ids)})

    elif payload.op == "cite_knowledge":
        # Store the knowledge document IDs the strategist cited in its analysis.
        # Mirrors cite_memos: the completion handler reads this to count
        # knowledge_cited reliably.  data must be {"cited_knowledge_ids": [...]}.
        if not isinstance(data, dict) or not isinstance(
            data.get("cited_knowledge_ids"), list
        ):
            raise HTTPException(
                status_code=400,
                detail='cite_knowledge: data must be {"cited_knowledge_ids": [...]}',
            )
        raw_ids = data["cited_knowledge_ids"]
        cited_ids = [
            str(i).strip()[:64]
            for i in raw_ids
            if isinstance(i, str) and str(i).strip()
        ]
        ctx = task.context or {}
        # NOTE: deliberately do NOT set _strategic_api_called here — mirrors the
        # cite_memos rationale: this op is auto-invoked before the executor pass
        # and must not mask a no-op cycle as a success.
        task.context = {
            **ctx,
            "_strategic_cited_knowledge_ids": cited_ids,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Stored {len(cited_ids)} cited knowledge id(s) for task {task.id}"
        )
        return _resp({"status": "ok", "op": "cite_knowledge", "count": len(cited_ids)})

    elif payload.op == "set_strategic_challenge":
        # Store the independent adversarial SECOND OPINION over the strategist's
        # own self-produced judgments (evidence_progress ratings, retirement
        # critiques, portfolio-shape decisions). The record is sanitized to a
        # bounded, versioned shape and stashed on task.context for the completion
        # handler to copy into StrategicCycle.cycle_metadata and for the first
        # hard gate (_evidence_progress_suppresses_scaffolding) to consult.
        #
        # NOTE: deliberately do NOT set _strategic_api_called here. A challenge is
        # a review of the agent's own judgments, NOT an executor mutation of
        # strategic state — flagging api-usage would let a challenge-only cycle
        # masquerade as a successful strategic mutation and suppress the no-op /
        # failure circuit-breaker. Mirrors the cite_memos rationale above.
        from app.strategic_completion import (
            sanitize_strategic_challenge_record,
        )

        record = sanitize_strategic_challenge_record(data)
        if record is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "set_strategic_challenge: data must be an object with optional "
                    "evidence_progress / retirement_approvals / "
                    "retirement_critiques / portfolio_shape / portfolio_operations lists"
                ),
            )
        ctx = task.context or {}
        task.context = {
            **ctx,
            STRATEGIC_CHALLENGE_CONTEXT_KEY: record,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        ep_n = len(record.get("evidence_progress", []))
        ra_n = len(record.get("retirement_approvals", []))
        rc_n = 1 if "retirement_critique_quality" in record else 0
        ps_n = 1 if "portfolio_shape_risk" in record else 0
        po_n = len(record.get("portfolio_operations", []))
        logger.info(
            "[STRATEGIC API] Stored strategic judgment challenge for task %s "
            "(evidence_progress=%d, retirement_approvals=%d, "
            "retirement_critique_quality=%d, portfolio_shape_risk=%d, "
            "portfolio_operations=%d)",
            task.id,
            ep_n,
            ra_n,
            rc_n,
            ps_n,
            po_n,
        )
        version = record.get("challenge_version", record.get("version"))
        return _resp(
            {
                "status": "ok",
                "op": "set_strategic_challenge",
                "version": version,
                "challenge_version": version,
                "challenge_status": record.get("status"),
                "evidence_progress_count": ep_n,
                "retirement_approvals_count": ra_n,
                "retirement_critique_quality_count": rc_n,
                "portfolio_shape_risk_count": ps_n,
            }
        )

    elif payload.op == "set_acceptance_criteria":
        # Author criteria for the open release version and optionally map
        # directions to them.  Restricted to strategic_review agents (enforced
        # above by the task_type guard).  Deduplicates by text within the
        # (project_id, release_version_id) scope so repeated calls are safe.
        from app.acceptance_criteria import (
            count_active_criteria_for_version as _count_active_criteria,
            get_criteria_by_keys as _get_criteria_by_keys,
            get_criterion_by_text as _get_criterion_by_text,
            generate_criterion_key as _gen_criterion_key,
            is_operator_authored_criterion as _is_operator_authored_criterion,
            MAX_CRITERIA_PER_VERSION as _MAX_CRITERIA,
            OperatorAuthoredRetractionError as _OperatorAuthoredRetractionError,
            retract_criterion as _retract_criterion,
        )
        from app.notification_dispatch import (
            post_criterion_retracted_message as _post_criterion_retracted_message,
            post_criterion_superseded_message as _post_criterion_superseded_message,
        )
        from app.directions_repo import get_direction_row as _get_direction_row
        from app.models import VersionAcceptanceCriterion as _VAC
        from app.release_version import current_open_version as _cur_open_ver

        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400,
                detail="set_acceptance_criteria: data must be an object",
            )
        criteria_input = data.get("criteria", [])
        if not isinstance(criteria_input, list):
            raise HTTPException(
                status_code=400,
                detail="set_acceptance_criteria: 'criteria' must be a list when provided",
            )
        for item in criteria_input:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("text"), str)
                or not item["text"].strip()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="set_acceptance_criteria: each criterion must have a non-empty 'text' string",
                )
        retract_input = data.get("retract", [])
        if not isinstance(retract_input, list):
            raise HTTPException(
                status_code=400,
                detail="set_acceptance_criteria: 'retract' must be a list when provided",
            )
        for item in retract_input:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("criterion_key"), str)
                or not item["criterion_key"].strip()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="set_acceptance_criteria: each retract item must have a non-empty 'criterion_key' string",
                )
            if (
                not isinstance(item.get("reason"), str)
                or not item["reason"].strip()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="set_acceptance_criteria: each retract item must have a non-empty 'reason' string",
                )
            superseded_by_key = item.get("superseded_by_key")
            if superseded_by_key is not None and not isinstance(
                superseded_by_key, str
            ):
                raise HTTPException(
                    status_code=400,
                    detail="set_acceptance_criteria: 'superseded_by_key' must be a string when provided",
                )

        open_version = await _cur_open_ver(session, project.id)
        if open_version is None:
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(
                content=_resp({"status": "error", "reason": "No open release version"}),
                status_code=409,
            )

        # LOOP-AUTHORSHIP GUARD. Validate ALL retract targets up front so a
        # strategist/loop-driven retraction can only touch criteria the loop
        # authored. Operator/human-authored criteria (created_by=operator/user/
        # human) require operator action; refuse the WHOLE request before any
        # retraction commits so a partial batch cannot leave some retracted.
        # Also snapshot each target's pre-retraction status so the notification
        # step only fires when a previously-active criterion actually changes.
        # Batch-fetch ALL retraction targets (and supersession target keys) in
        # a single query instead of N individual _get_criterion_by_key() calls
        # across three separate loops (finding 1: 3N → 1 + N).
        _retract_keys: list[str] = [item["criterion_key"].strip() for item in retract_input]
        _supersession_keys: list[str] = [
            item["superseded_by_key"].strip()
            for item in retract_input
            if isinstance(item.get("superseded_by_key"), str) and item["superseded_by_key"].strip()
        ]
        _all_crit_keys = list(set(_retract_keys + _supersession_keys))
        _criteria_by_key: dict[str, _VAC] = {}
        if _all_crit_keys:
            _rows = await _get_criteria_by_keys(
                session, project.id, _all_crit_keys, release_version_id=open_version.id
            )
            _criteria_by_key = {r.criterion_key: r for r in _rows}

        pre_status_by_key: dict[str, str | None] = {}
        for item in retract_input:
            criterion_key = item["criterion_key"].strip()
            existing = _criteria_by_key.get(criterion_key)
            pre_status_by_key[criterion_key] = (
                existing.status if existing is not None else None
            )
            if existing is not None and _is_operator_authored_criterion(
                existing.created_by
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"set_acceptance_criteria: criterion '{criterion_key}' was "
                        f"authored by an operator and cannot be retracted by the "
                        f"autonomous loop; operator action is required to retract it"
                    ),
                )

        # Validate supersession targets before executing any retractions.
        for item in retract_input:
            criterion_key = item["criterion_key"].strip()
            superseded_by_key_raw = item.get("superseded_by_key")
            superseded_by_key = (
                superseded_by_key_raw.strip()
                if isinstance(superseded_by_key_raw, str) and superseded_by_key_raw.strip()
                else None
            )
            if superseded_by_key is None:
                continue
            if superseded_by_key == criterion_key:
                raise HTTPException(
                    status_code=400,
                    detail=f"set_acceptance_criteria: criterion '{criterion_key}' cannot supersede itself",
                )
            target = _criteria_by_key.get(superseded_by_key)
            if target is None or target.status != "active":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"set_acceptance_criteria: superseded_by_key '{superseded_by_key}' "
                        f"does not refer to an active criterion on the open release version"
                    ),
                )

        # Process retractions FIRST so freed headroom is available to additions
        # in the same call — this is the core safety-valve path.
        retracted: list[dict] = []
        retracted_active_count = 0
        for item in retract_input:
            criterion_key = item["criterion_key"].strip()
            reason = item["reason"].strip()
            superseded_by_key_raw = item.get("superseded_by_key")
            superseded_by_key = (
                superseded_by_key_raw.strip()
                if isinstance(superseded_by_key_raw, str)
                and superseded_by_key_raw.strip()
                else None
            )
            try:
                row = await _retract_criterion(
                    session,
                    project_id=project.id,
                    release_version_id=open_version.id,
                    criterion_key=criterion_key,
                    reason=reason,
                    retracted_by="strategist",
                    superseded_by_key=superseded_by_key,
                    enforce_loop_authored=True,
                )
            except _OperatorAuthoredRetractionError as exc:
                # Belt-and-suspenders: the up-front guard already rejected
                # operator-authored targets, so reaching here means state
                # changed under us. Refuse cleanly rather than partially commit.
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            took_effect = (
                row is not None
                and row.status != "active"
                and pre_status_by_key.get(criterion_key) == "active"
            )
            if took_effect:
                # Row was active and is now retracted/superseded — headroom freed.
                retracted_active_count += 1
                # OPERATOR-ATTENTION SIGNAL. Best-effort: a failure to post the
                # notification must NEVER abort or roll back the retraction. The
                # message rides the caller's transaction (post_event_message only
                # session.add()s; the commit below flushes it).
                try:
                    if superseded_by_key:
                        await _post_criterion_superseded_message(
                            session,
                            project.id,
                            criterion_key=criterion_key,
                            text=row.text,
                            reason=reason,
                            release_version_id=open_version.id,
                            superseded_by_key=superseded_by_key,
                            retracted_by="strategist",
                            task_id=task.id,
                        )
                    else:
                        await _post_criterion_retracted_message(
                            session,
                            project.id,
                            criterion_key=criterion_key,
                            text=row.text,
                            reason=reason,
                            release_version_id=open_version.id,
                            retracted_by="strategist",
                            task_id=task.id,
                        )
                except Exception as exc:  # noqa: BLE001 — signal is best-effort
                    logger.warning(
                        "[STRATEGIC API] Failed to post criterion "
                        "retraction/supersession notification for %s: %s",
                        criterion_key,
                        exc,
                    )
            retracted.append(
                {
                    "criterion_key": criterion_key,
                    "status": row.status if row is not None else None,
                    "found": row is not None,
                }
            )

        # Active-only count minus what we just retracted in this call
        existing_active_count = await _count_active_criteria(session, open_version.id)
        # Snapshot active criterion keys from DB to use for stale-key scrubbing
        # when merging criterion_key into dir_row.criterion_keys.  Keys created
        # in this call are tracked separately so multi-item loops don't lose
        # each other's newly written keys before they flush to DB.
        from app.direction_referential_guard import (
            load_version_criterion_key_sets as _load_crit_key_sets,
        )
        _db_active_crit_keys, _ = await _load_crit_key_sets(session, project.id, open_version.id)
        _call_crit_keys: set[str] = set()
        new_added_count = 0
        skipped_over_cap = 0
        processed: list[dict] = []

        # Batch-fetch ALL direction rows referenced across criteria_input in a
        # single query instead of per-item N+1 _get_direction_row() calls
        # (finding 2: M*D → 1 query).
        _all_direction_keys: list[str] = []
        for item in criteria_input:
            _all_direction_keys.extend(item.get("direction_keys") or [])
        _direction_rows: dict[str, Any] = {}
        if _all_direction_keys:
            _unique_dir_keys = list(set(_all_direction_keys))
            from app.models import Direction as _DirectionModel
            _dir_result = await session.execute(
                select(_DirectionModel).where(
                    _DirectionModel.project_id == project.id,
                    _DirectionModel.direction_key.in_(_unique_dir_keys),
                )
            )
            _direction_rows = {r.direction_key: r for r in _dir_result.scalars().all()}

        for item in criteria_input:
            text = item["text"].strip()
            direction_keys_input: list[str] = list(item.get("direction_keys") or [])

            existing = await _get_criterion_by_text(
                session, project.id, open_version.id, text
            )
            if existing is not None:
                criterion_key = existing.criterion_key
                is_new = False
            else:
                if existing_active_count + new_added_count >= _MAX_CRITERIA:
                    skipped_over_cap += 1
                    continue
                criterion_key = _gen_criterion_key()
                new_row = _VAC(
                    project_id=project.id,
                    release_version_id=open_version.id,
                    criterion_key=criterion_key,
                    text=text,
                    created_by="strategist",
                )
                session.add(new_row)
                new_added_count += 1
                is_new = True

            # Track keys added in this call so later iterations can preserve
            # them even before they flush to DB.
            _call_crit_keys.add(criterion_key)

            mapped_keys: list[str] = []
            for dk in direction_keys_input:
                dir_row = _direction_rows.get(dk)
                if dir_row is None:
                    warnings.append(f"Unknown direction_key: {dk}")
                    continue
                # Scrub stale (inactive/old-version) keys from the existing set,
                # keeping only keys that are active in this version or were
                # added during this call (not yet flushed to DB).
                _valid_keys = _db_active_crit_keys | _call_crit_keys
                _prior_keys = set(dir_row.criterion_keys or [])
                merged = list(
                    (_valid_keys & _prior_keys) | {criterion_key}
                )
                _scrubbed = _prior_keys - _valid_keys - {criterion_key}
                if _scrubbed:
                    warnings.append(
                        f"Direction '{dk}': {len(_scrubbed)} stale criterion key(s) "
                        f"removed: {sorted(_scrubbed)}"
                    )
                dir_row.criterion_keys = merged
                _dir_data = dict(dir_row.data or {})
                _dir_data["criterion_release_version_id"] = str(open_version.id)
                dir_row.data = _dir_data
                session.add(dir_row)
                mapped_keys.append(dk)

            processed.append(
                {
                    "criterion_key": criterion_key,
                    "text": text,
                    "is_new": is_new,
                    "direction_keys": mapped_keys,
                }
            )

        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_api_called": True,
            "_strategic_criteria_authored": len(processed),
            "_strategic_criteria_retracted": sum(
                1 for item in retracted if item["found"]
            ),
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            "[STRATEGIC API] set_acceptance_criteria for task %s: %d criteria, "
            "%d retracted, %d skipped (cap), %d warnings",
            task.id,
            len(processed),
            sum(1 for item in retracted if item["found"]),
            skipped_over_cap,
            len(warnings),
        )

        # Best-effort backfill: for each direction that received new criterion
        # mappings, replay merge evidence for tasks that already landed.
        _mapped_directions: set[str] = set()
        for item in processed:
            for dk in item.get("direction_keys", []):
                _mapped_directions.add(dk)
        if _mapped_directions:
            from app.acceptance_criteria import backfill_direction_merge_evidence as _backfill
            for _dk in _mapped_directions:
                try:
                    await _backfill(session, project.id, _dk)
                except Exception:  # noqa: BLE001, PERF203
                    logger.warning(
                        "[STRATEGIC API] set_acceptance_criteria backfill failed "
                        "for direction %s (non-fatal)",
                        _dk,
                        exc_info=True,
                    )

        return _resp(
            {
                "status": "ok",
                "op": "set_acceptance_criteria",
                "criteria": processed,
                "retracted": retracted,
                "skipped_over_cap": skipped_over_cap,
            }
        )

    elif payload.op == "dispose_completion_debt":
        # Strategist disposition of an open completion-debt row: resolve it (with
        # required evidence) or dismiss it (with a reason). Scoped to this
        # project so a strategist can never touch another project's ledger.
        from app.completion_debt import (
            debt_to_dict as _debt_to_dict,
            dismiss_debt as _dismiss_debt,
            resolve_debt as _resolve_debt,
        )

        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="dispose_completion_debt data must be an object"
            )
        debt_id = data.get("debt_id")
        if not debt_id:
            raise HTTPException(status_code=400, detail="debt_id is required")
        disposition = str(data.get("disposition", "")).strip().lower()
        if disposition not in ("resolved", "dismissed"):
            raise HTTPException(
                status_code=400,
                detail="disposition must be one of ['resolved', 'dismissed']",
            )
        reason = str(data.get("reason") or data.get("evidence") or "").strip()
        # Resolving a debt REQUIRES evidence — a bare "resolved" with no proof is
        # exactly the green-CI churn the ledger exists to prevent. Dismissal also
        # requires a reason (enforced by dismiss_debt).
        if disposition == "resolved" and not reason:
            raise HTTPException(
                status_code=400,
                detail="resolving completion debt requires evidence (reason)",
            )
        try:
            if disposition == "resolved":
                entry = await _resolve_debt(
                    session, debt_id, project_id=project.id, reason=reason
                )
            else:
                entry = await _dismiss_debt(
                    session, debt_id, project_id=project.id, reason=reason
                )
        except ValueError as exc:
            if "not_found" in str(exc):
                raise HTTPException(
                    status_code=404, detail="Completion debt entry not found"
                ) from exc
            if "invalid_reason" in str(exc):
                raise HTTPException(
                    status_code=400, detail="reason must be a non-empty string"
                ) from exc
            raise HTTPException(status_code=409, detail="Entry is not open") from exc

        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return _resp(
            {
                "status": "ok",
                "op": "dispose_completion_debt",
                "disposition": disposition,
                "entry": _debt_to_dict(entry),
            }
        )

    elif payload.op == "consume_knowledge_followup":
        # Strategist marks a pending knowledge follow-up (design/web_research
        # doc) as consumed — meaning the strategist has acted on it (spawned a
        # direction, incorporated the knowledge, etc.).
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="consume_knowledge_followup data must be an object"
            )
        document_id = data.get("document_id")
        if not document_id:
            raise HTTPException(
                status_code=400, detail="document_id is required"
            )
        try:
            doc_id_uuid = uuid.UUID(str(document_id))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="document_id must be a valid UUID"
            ) from None
        doc = (
            (await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == doc_id_uuid,
                    KnowledgeDocument.project_id == project.id,
                )
            ))
            .scalars()
            .first()
        )
        if doc is None:
            raise HTTPException(
                status_code=404, detail="Knowledge document not found"
            )
        if doc.lifecycle_status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Cannot consume: lifecycle_status is '{doc.lifecycle_status}' (must be 'pending')",
            )
        doc.lifecycle_status = "consumed"
        doc.consumed_at = _utcnow()
        session.add(doc)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await session.refresh(doc)
        return _resp({
            "status": "ok",
            "op": "consume_knowledge_followup",
            "document_id": str(doc.id),
            "lifecycle_status": doc.lifecycle_status,
        })

    elif payload.op == "dismiss_knowledge_followup":
        # Strategist marks a pending knowledge follow-up as dismissed — meaning
        # it's not actionable right now.
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="dismiss_knowledge_followup data must be an object"
            )
        document_id = data.get("document_id")
        if not document_id:
            raise HTTPException(
                status_code=400, detail="document_id is required"
            )
        reason = str(data.get("reason") or "").strip()
        if not reason:
            raise HTTPException(
                status_code=400, detail="reason is required (why not actionable)"
            )
        try:
            doc_id_uuid = uuid.UUID(str(document_id))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="document_id must be a valid UUID"
            ) from None
        doc = (
            (await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == doc_id_uuid,
                    KnowledgeDocument.project_id == project.id,
                )
            ))
            .scalars()
            .first()
        )
        if doc is None:
            raise HTTPException(
                status_code=404, detail="Knowledge document not found"
            )
        if doc.lifecycle_status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Cannot dismiss: lifecycle_status is '{doc.lifecycle_status}' (must be 'pending')",
            )
        doc.lifecycle_status = "dismissed"
        doc.dismissed_at = _utcnow()
        doc.dismissed_reason = reason
        session.add(doc)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await session.refresh(doc)
        return _resp({
            "status": "ok",
            "op": "dismiss_knowledge_followup",
            "document_id": str(doc.id),
            "lifecycle_status": doc.lifecycle_status,
        })

    elif payload.op == "record_finding_resolution":
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="record_finding_resolution data must be an object"
            )
        finding_key = str(data.get("finding_key") or "").strip()
        state = str(data.get("state") or "").strip()
        # Coerce optional fields to str before they reach the model columns —
        # the dedicated endpoint gets this typing from AuditFindingResolutionOp,
        # but this arm receives untyped JSON (e.g. severity: 5).
        severity = str(data.get("severity") or "").strip() or None
        note = str(data.get("note") or "").strip() or None
        resolving_ref = str(data.get("resolving_ref") or "").strip() or None
        verified_against_sha = str(data.get("verified_against_sha") or "").strip() or None
        task_type = token.get("task_type")
        resolution_version_id = await _apply_finding_resolution_write(
            session,
            task,
            project.id,
            finding_key,
            state,
            task_type,
            severity=severity,
            note=note,
            resolving_ref=resolving_ref,
            verified_against_sha=verified_against_sha,
        )
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            "[STRATEGIC API] record_finding_resolution: task %s marked finding '%s' as '%s' "
            "for project %s version %s",
            task.id,
            finding_key,
            state,
            project.id,
            resolution_version_id,
        )
        return _resp(
            {
                "status": "ok",
                "op": "record_finding_resolution",
                "finding_key": finding_key,
                "state": state,
                "release_version_id": str(resolution_version_id),
            }
        )

    elif payload.op == "set_session_summary":
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400,
                detail="set_session_summary data must be an object with a 'summary' field",
            )
        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise HTTPException(
                status_code=400,
                detail="summary text is required",
            )
        # Bounded to 10 KB to keep JSONB columns lean
        _MAX_SUMMARY_BYTES = 10_000
        summary_bytes = summary.encode("utf-8")
        if len(summary_bytes) > _MAX_SUMMARY_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Session summary too large ({len(summary_bytes)} bytes, max {_MAX_SUMMARY_BYTES})",
            )

        # Write to task context so the running agent reads it on bootstrap
        ctx = dict(task.context or {})
        ctx["_session_summary"] = summary
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)

        await session.commit()
        return _resp({"status": "ok", "op": "set_session_summary"})

    elif payload.op == "watch_task":
        try:
            watch_input = WatchTaskInput.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"watch_task: invalid data: {exc}",
            ) from None
        try:
            watch, created = await register_task_watch(
                session,
                project_id=project.id,
                created_by_task_id=task.id,
                target_task_id=watch_input.target_task_id,
                purpose=watch_input.purpose.strip(),
                strategic_session_id=str(
                    (task.context or {}).get("strategic_session_id") or ""
                )
                or None,
                deadline_at=watch_input.deadline_at,
            )
        except ValueError as exc:
            status_code = (
                400 if "cannot watch itself" in str(exc) else 404
            )
            raise HTTPException(status_code=status_code, detail=str(exc)) from None
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return _resp(
            {
                "status": "ok",
                "op": "watch_task",
                "created": created,
                "watch": serialize_watch(watch),
            }
        )

    elif payload.op == "ack_watch":
        try:
            ack_input = AcknowledgeWatchInput.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"ack_watch: invalid data: {exc}",
            ) from None
        try:
            watch = await acknowledge_watch(
                session,
                project_id=project.id,
                watch_id=ack_input.watch_id,
                disposition=ack_input.disposition,
                note=ack_input.note.strip(),
            )
        except ValueError as exc:
            status_code = 404 if "not found in project" in str(exc) else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from None
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return _resp(
            {
                "status": "ok",
                "op": "ack_watch",
                "watch": serialize_watch(watch),
            }
        )

    elif payload.op == "cancel_watch":
        try:
            cancel_input = CancelWatchInput.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"cancel_watch: invalid data: {exc}",
            ) from None
        try:
            watch = await cancel_watch(
                session,
                project_id=project.id,
                watch_id=cancel_input.watch_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return _resp(
            {
                "status": "ok",
                "op": "cancel_watch",
                "watch": serialize_watch(watch),
            }
        )

    elif payload.op == "cancel_task":
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400,
                detail="cancel_task data must be an object",
            )
        target_task_id = data.get("target_task_id")
        spawn_request_id = data.get("request_id")
        reason = str(data.get("reason", "")).strip()
        if bool(target_task_id) == bool(spawn_request_id):
            raise HTTPException(
                status_code=400,
                detail="cancel_task requires exactly one of 'target_task_id' or 'request_id'",
            )
        if not reason:
            raise HTTPException(
                status_code=400,
                detail="cancel_task requires a non-empty 'reason' string",
            )
        reason = _cap_str(reason, 500, "reason", warnings)

        if spawn_request_id:
            try:
                request_uuid = uuid.UUID(str(spawn_request_id))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="cancel_task request_id must be a UUID",
                ) from exc
            request = await session.get(StrategicSpawnRequest, request_uuid)
            if request is None or request.project_id != project.id:
                raise HTTPException(
                    status_code=404,
                    detail=f"cancel_task: spawn request {spawn_request_id} not found",
                )
            if request.parent_task_id != task.id:
                raise HTTPException(
                    status_code=403,
                    detail="cancel_task: spawn request was not created by this strategic session",
                )
            try:
                request = await cancel_deferred_spawn_request(
                    session,
                    project_id=project.id,
                    request_id=request_uuid,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            request.policy_snapshot = {
                **dict(request.policy_snapshot or {}),
                "cancel_reason": reason,
            }
            session.add(request)
            await session.commit()
            return _resp(
                {
                    "status": "ok",
                    "op": "cancel_task",
                    "request_id": str(request.id),
                    "request_state": request.state,
                }
            )

        target_task = await session.get(Task, target_task_id)
        if not target_task:
            raise HTTPException(
                status_code=404,
                detail=f"cancel_task: target task {target_task_id} not found",
            )
        if target_task.project_id != project.id:
            raise HTTPException(
                status_code=403,
                detail="cancel_task: target task belongs to a different project",
            )

        # Only allow cancelling tasks that THIS strategic session spawned.
        target_ctx = target_task.context or {}
        if target_ctx.get("parent_strategic_task_id") != str(task.id):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"cancel_task: target task {target_task_id} was not spawned "
                    f"by this strategic session ({task.id})"
                ),
            )

        # Only allow cancelling early-lifecycle states (no active VM/agent).
        _CANCELLABLE_STATUSES = frozenset(
            {"QUEUED", "PROVISIONING"}
        )
        _target_status = target_task.status.value if hasattr(target_task.status, 'value') else str(target_task.status)
        if _target_status not in _CANCELLABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cancel_task: target task {target_task_id} is in "
                    f"status '{_target_status}', which is not cancellable "
                    f"(allowed: {sorted(_CANCELLABLE_STATUSES)})"
                ),
            )

        # Transition to CANCELLED with reason recorded durably.
        previous_status = _target_status
        target_ctx = dict(target_task.context or {})
        target_ctx["cancelled_by"] = "STRATEGIC_AGENT"
        target_ctx["cancelled_by_task_id"] = str(task.id)
        target_ctx["cancel_reason"] = reason
        target_ctx["cancelled_at"] = _utcnow().isoformat()
        target_task.context = target_ctx
        target_task.status = TaskStatus.CANCELLED
        target_task.latest_status_message = (
            f"Cancelled by strategic session {task.id}: {reason}"
        )
        flag_modified(target_task, "context")
        session.add(target_task)

        # Stamp the parent review so the cycle knows it cancelled work.
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()

        logger.info(
            "[STRATEGIC API] cancel_task: task %s cancelled %s "
            "(reason=%s) for project %s",
            task.id,
            target_task_id,
            reason,
            project.id,
        )
        return _resp(
            {
                "status": "ok",
                "op": "cancel_task",
                "target_task_id": str(target_task.id),
                "previous_status": previous_status,
            }
        )

    elif payload.op == "consume_knowledge":
        # Mark a knowledge document as consumed — the strategist has read and
        # acted on it. Once consumed, it is excluded from the pending-follow-up
        # surface in the strategic context package evidence section.
        if not isinstance(data, dict) or not isinstance(
            data.get("knowledge_document_id"), str
        ) or not data["knowledge_document_id"].strip():
            raise HTTPException(
                status_code=400,
                detail='consume_knowledge: data must be {"knowledge_document_id": "<uuid>"}',
            )
        raw_id = data["knowledge_document_id"].strip()
        try:
            doc_id = uuid.UUID(raw_id)
        except (ValueError, AttributeError):
            raise HTTPException(  # noqa: B904
                status_code=400,
                detail=f"consume_knowledge: invalid knowledge_document_id: {raw_id}",
            )
        doc = await get_knowledge_document(session, project.id, doc_id)
        if doc is None:
            raise HTTPException(
                status_code=404,
                detail=f"Knowledge document {doc_id} not found in this project",
            )
        doc.lifecycle_status = "consumed"
        doc.consumed_at = _utcnow()
        session.add(doc)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            "[STRATEGIC API] Consumed knowledge document %s for project %s",
            doc_id,
            project.id,
        )
        return _resp(
            {
                "status": "ok",
                "op": "consume_knowledge",
                "document_id": str(doc_id),
            }
        )

    elif payload.op == "dismiss_knowledge":
        # Mark a knowledge document as dismissed — the strategist has reviewed
        # it and determined it is not actionable. An optional reason can be
        # recorded for auditability.
        if not isinstance(data, dict) or not isinstance(
            data.get("knowledge_document_id"), str
        ) or not data["knowledge_document_id"].strip():
            raise HTTPException(
                status_code=400,
                detail='dismiss_knowledge: data must be {"knowledge_document_id": "<uuid>", "reason": "<optional string>"}',
            )
        raw_id = data["knowledge_document_id"].strip()
        try:
            doc_id = uuid.UUID(raw_id)
        except (ValueError, AttributeError):
            raise HTTPException(  # noqa: B904
                status_code=400,
                detail=f"dismiss_knowledge: invalid knowledge_document_id: {raw_id}",
            )
        doc = await get_knowledge_document(session, project.id, doc_id)
        if doc is None:
            raise HTTPException(
                status_code=404,
                detail=f"Knowledge document {doc_id} not found in this project",
            )
        reason = str(data.get("reason") or "").strip() or None
        doc.lifecycle_status = "dismissed"
        doc.dismissed_at = _utcnow()
        doc.dismissed_reason = (
            reason[:500] if reason else None
        )
        session.add(doc)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            "[STRATEGIC API] Dismissed knowledge document %s for project %s (reason=%s)",
            doc_id,
            project.id,
            reason,
        )
        return _resp(
            {
                "status": "ok",
                "op": "dismiss_knowledge",
                "document_id": str(doc_id),
            }
        )

    elif payload.op == "request_qualification":
        # Dedicated one-off qualification trigger. Does NOT touch ProblemModel at
        # all — it calls the same underlying qualify logic that set_problem_model
        # uses via its qualify_request rider, but reached directly so the agent
        # (or operator) can request a fresh qualification run without having to
        # construct a set_problem_model payload.
        #
        # Data shape: {"reason": "<string>"}. reason is optional — an absent or
        # empty reason is treated as "". Wrong-phase is a no-op (same behavior as
        # qualify_request via set_problem_model). The qualification runs
        # post-commit on its own fresh session for the same structural reason as
        # the set_problem_model path: avoid holding any DB lock during slow GitHub
        # I/O (CI-compliance scan + run_qualification).
        from app.strategic_completion import (
            _apply_qualify_request_post_commit,
            extract_qualify_request,
        )

        qualify_request = extract_qualify_request(
            data if isinstance(data, dict) else {}
        )
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        _rq_task_id = task.id
        _rq_project_id = project.id
        await session.commit()
        await _apply_qualify_request_post_commit(
            qualify_request,
            task_id=_rq_task_id,
            project_id=_rq_project_id,
            path="api_inline_request_qualification",
        )
        logger.info(
            "[STRATEGIC API] request_qualification applied for task %s project %s "
            "(reason=%r) — release qualifier ran if open version is in stabilize",
            _rq_task_id,
            _rq_project_id,
            (qualify_request or {}).get("reason"),
        )
        return _resp(
            {
                "status": "ok",
                "op": "request_qualification",
                "reason_received": (qualify_request or {}).get("reason") or None,
            }
        )

    else:
        raise HTTPException(status_code=400, detail=f"Unknown op: {payload.op}")


# ---------------------------------------------------------------------------
# Agent write op: audit finding resolution
# ---------------------------------------------------------------------------

# Task types that perform fix-first verification and are authorized to record
# audit finding resolution state.
_AUDIT_FINDING_RESOLUTION_TASK_TYPES: frozenset[str] = frozenset(
    {
        "release_audit",
        "release_audit_challenge",
        "research",
        "strategic_review",
    }
)

_VALID_RESOLUTION_STATES: frozenset[str] = frozenset({"resolved", "stale"})

# Task types that carry a stable release_version_id in their context (stamped
# at spawn time) and must be checked for version staleness before writing.
_AUDIT_BOUND_VERSION_TASK_TYPES: frozenset[str] = frozenset(
    {"release_audit", "release_audit_challenge"}
)


async def _apply_finding_resolution_write(
    session: AsyncSession,
    task: Task,
    project_id: uuid.UUID,
    finding_key: str,
    state: str,
    task_type: str | None,
    severity: str | None = None,
    note: str | None = None,
    resolving_ref: str | None = None,
    verified_against_sha: str | None = None,
) -> uuid.UUID:
    """Validate, version-bind, and apply an audit finding resolution.

    Shared by both the dedicated ``/api/agent/audit-finding-resolution``
    endpoint and the ``record_finding_resolution`` strategic op dispatch
    branch. Raises HTTPException on validation or version-binding failure.
    Returns the resolution_version_id on success; the caller must commit.
    """
    if state not in _VALID_RESOLUTION_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid state '{state}'; must be one of {sorted(_VALID_RESOLUTION_STATES)}",
        )
    if not finding_key or len(finding_key) > 64:
        raise HTTPException(
            status_code=400,
            detail="finding_key is required and must be at most 64 characters",
        )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")
    resolution_version_id = open_version.id
    # release_audit and release_audit_challenge tasks carry a stable
    # release_version_id in their context (stamped at spawn time). Use that
    # binding to attribute the resolution to the correct version, and reject
    # the write if the task's bound version is no longer the open one — an
    # older candidate must not mark findings resolved on a later open version.
    if task_type in _AUDIT_BOUND_VERSION_TASK_TYPES:
        bound_str = (task.context or {}).get("release_version_id")
        if bound_str:
            try:
                bound_id = uuid.UUID(bound_str)
            except (ValueError, AttributeError):
                raise HTTPException(  # noqa: B904
                    status_code=400,
                    detail="Invalid release_version_id in task context",
                )
            if bound_id != open_version.id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Task is bound to release version {bound_id} "
                        f"but the current open version is {open_version.id}; "
                        "resolution rejected to prevent cross-version attribution"
                    ),
                )
            resolution_version_id = bound_id
    from app.release_readiness import apply_finding_resolution

    await apply_finding_resolution(
        session,
        project_id,
        resolution_version_id,
        finding_key,
        state,
        severity=severity,
        note=note,
        resolving_ref=resolving_ref,
        verified_against_sha=verified_against_sha,
    )
    return resolution_version_id


class AuditFindingResolutionOp(BaseModel):
    task_id: uuid.UUID
    finding_key: str
    state: str  # 'resolved' | 'stale'
    severity: str | None = None
    note: str | None = None
    resolving_ref: str | None = None
    verified_against_sha: str | None = None


@router.post("/api/agent/audit-finding-resolution")
@limiter.limit("30/minute")
async def agent_audit_finding_resolution(
    request: Request,
    payload: AuditFindingResolutionOp,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent endpoint to mark a release audit finding resolved or stale."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    if token.get("task_type") not in _AUDIT_FINDING_RESOLUTION_TASK_TYPES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Task type '{token.get('task_type')}' is not authorized "
                "for audit finding resolution"
            ),
        )

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await _lock_project_or_429(session, task.project_id)
    task_type = token.get("task_type")

    resolution_version_id = await _apply_finding_resolution_write(
        session,
        task,
        project.id,
        payload.finding_key,
        payload.state,
        task_type,
        severity=payload.severity,
        note=payload.note,
        resolving_ref=payload.resolving_ref,
        verified_against_sha=payload.verified_against_sha,
    )
    await session.commit()

    logger.info(
        "[AUDIT_FINDING] task %s (%s) marked finding '%s' as '%s' "
        "for project %s version %s",
        task.id,
        task_type or "?",
        payload.finding_key,
        payload.state,
        project.id,
        resolution_version_id,
    )
    return {
        "status": "ok",
        "finding_key": payload.finding_key,
        "state": payload.state,
        "release_version_id": str(resolution_version_id),
    }


# ---------------------------------------------------------------------------
# Admin read endpoint: build-config confirmed commands
# ---------------------------------------------------------------------------


@router.get("/api/admin/projects/{project_id}/build-config/commands")
@limiter.limit("60/minute")
async def admin_get_build_config_commands(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return all directory entries with confirmed vs detected commands and staleness.

    Allows operators to audit what commands have been agent-confirmed without
    navigating the full build_config JSON.
    """
    from app.claim_task_helpers import is_commands_stale

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cfg = project.build_config or {}
    directories = cfg.get("directories") or []
    result = []
    for entry in directories:
        if not isinstance(entry, dict):
            continue
        confirmed_at = entry.get("commands_confirmed_at")
        result.append(
            {
                "path": entry.get("path"),
                "type": entry.get("type"),
                "detected_test_cmd": entry.get("test_cmd"),
                "detected_build_cmd": entry.get("build_cmd"),
                "detected_install_cmd": entry.get("install_cmd"),
                "detected_lint_cmd": entry.get("lint_cmd"),
                "confirmed_test_cmd": entry.get("confirmed_test_cmd"),
                "confirmed_build_cmd": entry.get("confirmed_build_cmd"),
                "confirmed_install_cmd": entry.get("confirmed_install_cmd"),
                "confirmed_lint_cmd": entry.get("confirmed_lint_cmd"),
                "commands_confirmed_at": confirmed_at,
                "confirming_task_id": entry.get("confirming_task_id"),
                "commands_stale": is_commands_stale(confirmed_at),
            }
        )

    # Surface the bootstrap command-memory outcome recorded separately from the
    # repo overview, so operators can distinguish a confirmed write-back from a
    # bootstrap that produced an overview but failed to confirm/write commands.
    # Absent on legacy projects (keys never stamped) -> command_memory is None.
    cm_status = cfg.get("bootstrap_command_memory_status")
    command_memory = None
    if cm_status is not None:
        command_memory = {
            "status": cm_status,
            "complete": cfg.get("bootstrap_command_memory_complete"),
            "detail": cfg.get("bootstrap_command_memory_detail"),
            "paths": cfg.get("bootstrap_command_memory_paths"),
            "updated_at": cfg.get("bootstrap_command_memory_updated_at"),
        }

    return {
        "project_id": str(project_id),
        "directories": result,
        "command_memory": command_memory,
    }


class _DeclareCapabilityVerdictRequest(BaseModel):
    cap_name: str
    verdict: Literal["adopt", "reject", "continue-trial"]
    rationale: str | None = None


@router.post("/api/admin/projects/{project_id}/capability-trials/declare")
@limiter.limit("30/minute")
async def admin_declare_capability_verdict(
    request: Request,
    project_id: uuid.UUID,
    body: _DeclareCapabilityVerdictRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Operator-declared capability trial verdict.

    Stores evidence_source='operator' so the write bypasses the automated
    self-revert and KILL-survival guards in record_trial_verdict.
    """
    project = await _lock_project_or_429(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cap_name = body.cap_name.strip()
    if not cap_name:
        raise HTTPException(
            status_code=400, detail="cap_name must be a non-empty string"
        )

    entry: dict[str, Any] = {
        "verdict": body.verdict,
        "evidence_source": "operator",
        "evaluated_at": _utcnow().isoformat(),
    }
    rationale_text = body.rationale.strip() if isinstance(body.rationale, str) else ""
    if rationale_text:
        entry["rationale"] = rationale_text

    record_trial_verdict(project, cap_name, entry)

    # Close any open stall notice for this capability when a decided verdict
    # (adopt/reject) is declared. continue-trial leaves the notice open because
    # the trial is still unresolved.
    if body.verdict in ("adopt", "reject"):
        dedupe_key = f"capability_trial_stalled_{cap_name}"
        stall_msgs = (
            (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.status == "open",
                        ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
                    )
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        for msg in stall_msgs:
            msg.status = "resolved"
            msg.resolved_at = _utcnow()
            session.add(msg)

    await session.commit()

    logger.info(
        "[ADMIN] Operator declared capability verdict for %s=%s on project %s",
        cap_name,
        body.verdict,
        project_id,
    )
    return {
        "status": "ok",
        "cap_name": cap_name,
        "verdict": body.verdict,
    }


# ── Completion-debt endpoints ──────────────────────────────────────────────────


@router.get("/api/admin/projects/{project_id}/completion-debts")
@limiter.limit("60/minute")
async def admin_list_completion_debts(
    request: Request,
    project_id: uuid.UUID,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """List completion-debt entries for a project with optional filters."""
    from app.completion_debt import count_completion_debts, debt_to_dict, list_completion_debts

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    valid_statuses = {"open", "dismissed", "resolved"}
    if status is not None and status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(valid_statuses)}")

    entries = await list_completion_debts(
        session,
        project_id=project_id,
        status=status,
        kind=kind,
        limit=min(limit, 200),
        offset=offset,
    )
    total_count = await count_completion_debts(
        session,
        project_id=project_id,
        status=status,
        kind=kind,
    )
    return {"entries": [debt_to_dict(e) for e in entries], "count": len(entries), "total_count": total_count}


class _DismissDebtRequest(BaseModel):
    reason: str


@router.post("/api/admin/projects/{project_id}/completion-debts/{debt_id}/dismiss")
@limiter.limit("30/minute")
async def admin_dismiss_completion_debt(
    request: Request,
    project_id: uuid.UUID,
    debt_id: uuid.UUID,
    body: _DismissDebtRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Dismiss a completion-debt entry with a reason."""
    from app.completion_debt import debt_to_dict, dismiss_debt

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    reason = body.reason.strip() if isinstance(body.reason, str) else ""
    if not reason:
        raise HTTPException(status_code=400, detail="reason must be a non-empty string")

    try:
        entry = await dismiss_debt(session, debt_id, project_id=project_id, reason=reason)
    except ValueError as exc:
        if "not_found" in str(exc):
            raise HTTPException(status_code=404, detail="Completion debt entry not found") from exc
        raise HTTPException(status_code=409, detail="Entry is not open") from exc

    await session.commit()
    return {"status": "ok", "entry": debt_to_dict(entry)}


@router.post("/api/admin/projects/{project_id}/completion-debts/{debt_id}/resolve")
@limiter.limit("30/minute")
async def admin_resolve_completion_debt(
    request: Request,
    project_id: uuid.UUID,
    debt_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Mark a completion-debt entry as resolved."""
    from app.completion_debt import debt_to_dict, resolve_debt

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        entry = await resolve_debt(session, debt_id, project_id=project_id)
    except ValueError as exc:
        if "not_found" in str(exc):
            raise HTTPException(status_code=404, detail="Completion debt entry not found") from exc
        raise HTTPException(status_code=409, detail="Entry is not open") from exc

    await session.commit()
    return {"status": "ok", "entry": debt_to_dict(entry)}
