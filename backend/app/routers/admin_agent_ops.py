"""Agent operational endpoints: design-doc, playbook, annotate, strategic.

These handlers were extracted from app.main to keep that module under its
size cap. All HTTP routes, request/response schemas, and authentication rules
are unchanged.
"""

import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func as _func
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.database import get_session
from app.strategic_events import (
    STRATEGIC_KIND_DIRECTIONS,
    STRATEGIC_KIND_PROBLEM_MODEL,
    STRATEGIC_KIND_RUNNING_UPDATE,
    emit_strategic_updated,
)
from app.direction_lifecycle import _reset_outcome_detection_state
from app.models import MessageType, Project, ProjectMessage, Task, TaskStatus
from app.payload_schemas import (
    ALL_TASK_TYPES as _VALID_TASK_TYPES,
    validate_internal_payload as _validate_internal_payload,
)
from app.playbook_storage import (
    _PLAYBOOK_CAPACITY_BYTES,
    _playbook_size_bytes,
    maybe_archive_for_capacity as _maybe_archive_for_capacity,
)
from app.routers.agent import verify_agent_token
from app.source_ref_validation import (
    ground_design_doc_value,
    resolve_branch_verification_context as _resolve_branch_verification_context,
)
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    limiter,
    runtime_config as _runtime_config,
)
from app.size_limit_policy import bump_size_limit as _bump_size_limit_policy
from app.strategic_completion import (
    _find_matching_direction,
    _normalize_recommended_mode,
    _reset_scaffolding_state_on_outcome_change,
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
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.task_locking import (
    lock_project_or_429 as _lock_project_or_429,
    with_directions_locked as _with_directions_locked,
)
from app.time_utils import _utcnow

router = APIRouter()

logger = logging.getLogger(__name__)


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


class DesignDocUpdate(BaseModel):
    """Incremental design doc page update from an agent."""

    task_id: uuid.UUID
    op: str  # "set", "delete", "list"
    key: str | None = None
    value: Any | None = None


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
        return {"keys": [k for k in doc if not k.startswith("_")]}

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
        value = await ground_design_doc_value(
            value, payload.key, **verification_kwargs
        )
        return {"key": payload.key, "value": value}

    if not payload.key or not isinstance(payload.key, str):
        raise HTTPException(status_code=400, detail="key is required for set/delete")
    key = payload.key.strip()
    if not key or len(key) > 100 or key.startswith("_"):
        raise HTTPException(
            status_code=400, detail="Invalid key (max 100 chars, no _ prefix)"
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
        doc[key] = payload.value
        total_size = len(_json.dumps(doc))
        if total_size > 500_000:
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
                    sys_id = doc_key[len("systems/"):]
                    for fpath in doc_val.get("modules", []):
                        if isinstance(fpath, str):
                            file_to_system_map[fpath] = sys_id
            doc["_file_to_system"] = file_to_system_map
    elif payload.op == "delete":
        doc.pop(key, None)
        # Rebuild reverse index if a systems/* key is deleted to remove stale entries
        if key.startswith("systems/"):
            file_to_system_deleted_map: dict[str, str] = {}
            for doc_key, doc_val in doc.items():
                if doc_key.startswith("systems/") and isinstance(doc_val, dict):
                    sys_id = doc_key[len("systems/"):]
                    for fpath in doc_val.get("modules", []):
                        if isinstance(fpath, str):
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
            if isinstance(v, dict):
                pending_source = v.get("pending_source")
                if pending_source and pending_source != my_id:
                    continue
            keys.append(k[len("playbook/"):])
        return {"status": "ok", "op": op, "keys": keys}

    if op == "get":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        entries = {}
        my_id = str(task.id)
        for k, v in doc.items():
            if k.startswith("playbook/") and isinstance(v, dict):
                # Hide pending entries written by OTHER non-completed tasks —
                # only the source task can see its own un-finalised entries.
                # Prevents a failed task's lies from poisoning peers' prompts.
                pending_source = v.get("pending_source")
                if pending_source and pending_source != my_id:
                    continue
                short_key = k[len("playbook/"):]
                entries[short_key] = {
                    "content": v.get("content", ""),
                    "updated_at": v.get("updated_at", ""),
                    "source_task_id": v.get("source_task_id", ""),
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
        if len(payload.value) > 2048:
            raise HTTPException(
                status_code=400,
                detail=f"Value too large ({len(payload.value)} bytes, max 2KB)",
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
        entry: dict = {
            "content": payload.value,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": existing.get("confirmed_count", 0),
        }
        if payload.applicable_types is not None:
            entry["applicable_types"] = payload.applicable_types
        # Anti-poisoning: until the source task transitions to COMPLETED,
        # the entry is invisible to other tasks (filtered in the get/list
        # paths above and in prompt builders that surface playbook entries).
        # On COMPLETED, task_lifecycle clears the pending_source field; on
        # FAILED/CANCELLED, the entry is removed entirely. Prevents a task
        # that mid-flight claims "X exists" from poisoning peers' prompts
        # when the task ultimately fails to land the code.
        if task.status != TaskStatus.COMPLETED:
            entry["pending_source"] = str(task.id)
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
    file_path: str | None = None
    current_size: int | None = None
    change_summary: str | None = None


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

        new_limit = _bump_size_limit(current_size)
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

    msg = ProjectMessage(
        project_id=task.project_id,
        message_type=_ANNOTATION_TYPE_MAP[payload.annotation_type],
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
        response["new_limit"] = ctx["new_limit"]
    return response


class StrategicOp(BaseModel):
    """Incremental strategic review operation from an agent."""

    task_id: uuid.UUID
    op: str  # "set_vision", "create_direction", "update_direction", "retire_direction", "stage_direction", "create_message", "resolve_message", "spawn_task"
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
    # Direction/vision ops are restricted to strategic_review only.
    MESSAGE_OPS = ("create_message", "resolve_message")
    if payload.op not in MESSAGE_OPS and token.get("task_type") not in (
        "strategic_review",
    ):
        raise HTTPException(
            status_code=403,
            detail="Only strategic_review agents can modify directions/vision",
        )

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Lock project for atomic update (skip_locked + 429 avoids lock_timeout cascade)
    project = await _lock_project_or_429(session, task.project_id)

    data = payload.data
    warnings: List[str] = []

    # Size guard — prevent agents from bloating project JSONB columns
    if len(json.dumps(data, default=str)) > 100_000:
        raise HTTPException(
            status_code=400, detail="Data payload too large (max 100KB)"
        )

    def _resp(body: dict) -> dict:
        if warnings:
            body["warnings"] = list(warnings)
        return body

    async def _emit_updated(kind: str) -> None:
        """Post-commit refetch signal for a strategist output change.

        Must be awaited only AFTER the op's ``session.commit()``; the lock is
        already released by then, mirroring the established broadcast pattern.
        """
        await emit_strategic_updated(project.id, kind)

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
        success_evidence = str(data.get("success_evidence", "")).strip()
        retirement_condition = str(data.get("retirement_condition", "")).strip()
        if not success_evidence or not retirement_condition:
            raise HTTPException(
                status_code=400,
                detail="success_evidence and retirement_condition are required for direction creation",
            )
        # Atomic create-staged: when ``staged`` is set, the direction is born in
        # the "staged" state stamped to the CURRENT open version — identical
        # resolution semantics to ``stage_direction`` — so a next-version idea
        # never passes through a research-eligible active state (closing the old
        # create-then-stage race). Resolve the version FIRST so a missing open
        # version skips cleanly (409) without creating anything.
        staged_create = bool(data.get("staged"))
        staged_for_version: int | None = None
        if staged_create:
            from app.release_version import current_open_version  # noqa: PLC0415

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
            max_active = _runtime_config.get("max_active_directions", 5)
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
        if data.get("system_id"):
            new_dir["system_id"] = _cap_str(
                data["system_id"], STRATEGIC_SYSTEM_ID_MAX, "system_id", warnings
            )
        # Persist the direction→gap linkage so reconciliation can map an active
        # direction back to the problem-model gap it works on. Optional and
        # string-cast; additive on the JSONB direction dict (no migration).
        if data.get("gap_id"):
            new_dir["gap_id"] = str(data["gap_id"])
        if data.get("paths"):
            new_dir["paths"] = data["paths"]
        if data.get("category") and data["category"] in ("product", "structural"):
            new_dir["category"] = data["category"]
        if data.get("recommended_mode"):
            mode = _normalize_recommended_mode(data["recommended_mode"])
            if mode:
                new_dir["recommended_mode"] = mode
        directions.append(new_dir)
        _d = directions
        await _with_directions_locked(session, project.id, lambda _: _d)
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
        await _emit_updated(STRATEGIC_KIND_DIRECTIONS)
        logger.info(
            f"[STRATEGIC API] Created direction '{text[:60]}' "
            f"({'staged for v' + str(staged_for_version) if staged_create else 'active'}) "
            f"for project {project.id}"
        )
        resp_body: dict = {
            "status": "ok",
            "op": "create_direction",
            "direction_id": new_dir["id"],
        }
        if staged_create:
            resp_body["staged_for_version"] = staged_for_version
        return _resp(resp_body)

    elif payload.op == "update_direction":
        dir_id = data.get("direction_id")
        changes = data.get("changes", {})
        if not dir_id or not changes:
            raise HTTPException(
                status_code=400, detail="direction_id and changes required"
            )
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        directions = list(project.directions or [])
        updated = False
        for d in directions:
            if d.get("id") == dir_id:
                _pre_text = d.get("text")
                _pre_se = (d.get("context") or {}).get("success_evidence")
                _pre_rc = (d.get("context") or {}).get("retirement_condition")
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
                    if (
                        changes["status"] == "active"
                        and d.get("status") == "paused"
                        and not d.get("auto_paused")
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
                if "recommended_mode" in changes:
                    mode = _normalize_recommended_mode(changes["recommended_mode"])
                    if mode is not None:
                        d["recommended_mode"] = mode
                # Allow updating (or clearing) the direction→gap linkage so the
                # agent can re-ground an existing direction to a different gap.
                if "gap_id" in changes:
                    gid = changes["gap_id"]
                    if gid:
                        d["gap_id"] = str(gid)
                    else:
                        d.pop("gap_id", None)
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
                    notes.append(
                        {
                            "text": f"Strategic update: {rationale}",
                            "added_at": _utcnow().isoformat(),
                        }
                    )
                    d["notes"] = _cap_list(
                        notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings
                    )
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
        _d = directions
        await _with_directions_locked(session, project.id, lambda _: _d)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(STRATEGIC_KIND_DIRECTIONS)
        logger.info(f"[STRATEGIC API] Updated direction {dir_id}")
        return _resp({"status": "ok", "op": "update_direction", "direction_id": dir_id})

    elif payload.op == "retire_direction":
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        rationale = _cap_str(
            data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings
        )
        directions = list(project.directions or [])
        retired = False
        for d in directions:
            if d.get("id") == dir_id:
                d["status"] = "expired"
                notes = d.get("notes", [])
                notes.append(
                    {
                        "text": f"Retired by strategic review: {rationale}",
                        "added_at": _utcnow().isoformat(),
                    }
                )
                d["notes"] = _cap_list(
                    notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings
                )
                retired = True
                break
        if not retired:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        _d = directions
        await _with_directions_locked(session, project.id, lambda _: _d)
        ctx = task.context or {}
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
        await _emit_updated(STRATEGIC_KIND_DIRECTIONS)
        logger.info(f"[STRATEGIC API] Retired direction {dir_id}")
        # Advisory (non-blocking): each active direction is one parallel work-lane
        # the fleet fans out across, so retiring below the healthy floor caps max
        # parallelism and leaves VM capacity idle. ``directions`` already reflects
        # this retire (the row was flipped to "expired" above).
        _active_after = sum(
            1 for d in directions if d.get("status") == "active"
        )
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
        directions = list(project.directions or [])
        target = next((d for d in directions if d.get("id") == dir_id), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
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
        _d = directions
        await _with_directions_locked(session, project.id, lambda _: _d)
        ctx = task.context or {}
        staged_ids = list(ctx.get("_strategic_staged_ids", []))
        staged_ids.append(dir_id)
        task.context = {
            **ctx,
            "_strategic_dirs_staged": ctx.get("_strategic_dirs_staged", 0) + 1,
            "_strategic_staged_ids": staged_ids,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(STRATEGIC_KIND_DIRECTIONS)
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
        active_count = sum(1 for d in directions if d.get("status") == "active")
        max_active = _runtime_config.get("max_active_directions", 5)
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
        _d = directions
        await _with_directions_locked(session, project.id, lambda _: _d)
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_proposals_approved": ctx.get("_strategic_proposals_approved", 0)
            + 1,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(STRATEGIC_KIND_DIRECTIONS)
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
        # Cancel QUEUED tasks tied to this direction (same as the user DELETE flow)
        cancelled_result = await session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.QUEUED,
                Task.context["direction_id"].as_string() == dir_id,
            )
        )
        cancelled_tasks = cancelled_result.scalars().all()
        vm_ids_to_delete: List[str] = []
        for t in cancelled_tasks:
            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Cancelled: proposal {dir_id} was rejected by strategic review"
            )
            session.add(t)
            if t.vm_instance_id:
                vm_ids_to_delete.append(t.vm_instance_id)
        _d = remaining
        await _with_directions_locked(session, project.id, lambda _: _d)
        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_proposals_rejected": ctx.get("_strategic_proposals_rejected", 0)
            + 1,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        for vm_id in vm_ids_to_delete:
            # Lazy import avoids a circular dependency: main.py includes this
            # router, and _ensure_vm_deleted is defined in main.py.
            from app.vm_lifecycle import _ensure_vm_deleted as _main_ensure_vm_deleted
            await _main_ensure_vm_deleted(vm_id, reason="proposal_rejected")
        await _emit_updated(STRATEGIC_KIND_DIRECTIONS)
        logger.info(
            f"[STRATEGIC API] Rejected proposal {dir_id} for project {project.id} (cancelled {len(cancelled_tasks)} tasks)"
        )
        return _resp(
            {
                "status": "ok",
                "op": "reject_direction",
                "direction_id": dir_id,
                "cancelled_tasks": len(cancelled_tasks),
            }
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
        new_msg = ProjectMessage(
            project_id=project.id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=task.id,
            content=content,
            context=data.get("context") or {},
        )
        session.add(new_msg)
        await session.commit()
        # Broadcast so the UI can update immediately instead of waiting for poll
        await broadcast_to_project(
            str(project.id),
            "message_created",
            {
                "message_id": str(new_msg.id),
                "type": msg_type.value,
                "from": "strategic",
            },
        )
        return _resp(
            {"status": "ok", "op": "create_message", "message_id": str(new_msg.id)}
        )

    elif payload.op == "resolve_message":
        msg_id = data.get("message_id")
        if not msg_id:
            raise HTTPException(status_code=400, detail="message_id required")
        try:
            msg = await session.get(ProjectMessage, uuid.UUID(str(msg_id)))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="Invalid message_id"
            ) from None
        if not msg or msg.project_id != project.id:
            raise HTTPException(status_code=404, detail="Message not found")
        if msg.status == "resolved":
            return _resp({"status": "already_resolved"})
        msg.status = "resolved"
        resolution_raw = data.get("resolution", "Addressed by strategic review")
        msg.resolution = _cap_str(
            resolution_raw, STRATEGIC_RESOLUTION_MAX, "resolution", warnings
        )
        msg.resolved_at = _utcnow()
        session.add(msg)
        await session.commit()
        return _resp(
            {"status": "ok", "op": "resolve_message", "message_id": str(msg.id)}
        )

    elif payload.op == "spawn_task":
        # Allow strategic review to directly spawn a research or plan task for
        # targeted investigation, jumping the normal autonomous research queue.
        # Restricted to investigation types — implementation must go through
        # normal direction → research → plan → create flow.
        ALLOWED_SPAWN_TYPES = {"research", "plan"}
        spawn_type = data.get("type")
        if spawn_type not in ALLOWED_SPAWN_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"spawn_task type must be one of {sorted(ALLOWED_SPAWN_TYPES)}; got '{spawn_type}'",
            )
        raw_prompt = str(data.get("prompt", "")).strip()
        if not raw_prompt:
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

        # Build payload — use the same validator the research/plan spawners use
        # so the new task is indistinguishable from normal autonomous spawns
        # except for the context flags.
        spawn_payload: dict = {
            "type": spawn_type,
            "prompt": prompt,
            "title": title,
            "base_branch": _get_project_integration_branch(project),
        }
        if direction_id:
            spawn_payload["direction_id"] = direction_id
        # Apply per-type model/effort defaults
        type_model = _runtime_config.get("task_type_models", {}).get(spawn_type) or None
        type_effort = (
            _runtime_config.get("task_type_efforts", {}).get(spawn_type) or None
        )
        if type_model:
            spawn_payload["model"] = type_model
        if type_effort:
            spawn_payload["effort"] = type_effort
        spawn_payload = _validate_internal_payload(
            spawn_payload, f"strategic_spawn_task:{spawn_type}"
        )

        # Idempotency guard against retry-duplicated spawns.
        #
        # ``spawn_task`` is a non-idempotent POST, and the strategic agent's
        # shell helper (``_call`` in strategic-helpers.ts) retries on any
        # non-2xx — including its own ``curl --max-time 30`` timeout. When this
        # endpoint runs slow (e.g. event-loop saturation pushed one observed
        # call to 93s), the agent abandons the in-flight request and retries;
        # the abandoned request still commits server-side, so one logical spawn
        # produces two identical tasks (incident 2026-06-02: 00a63ec1 / 65108ef3
        # — same parent strategic review, byte-identical payload, 59s apart).
        #
        # The agent's retry is strictly sequential (timeout -> 5s sleep ->
        # retry), so the duplicate requests are never concurrent; a plain
        # read-then-skip closes the hole. Keying on the *source* strategic
        # task means a same-(type, title) match is always a true duplicate (a
        # single review run never legitimately re-spawns the identical task),
        # so this only collapses accidental retries — it does not constrain the
        # strategic agent's judgement across distinct reviews.
        dedup_since = _utcnow() - timedelta(minutes=30)
        dupe_stmt = (
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.created_by == "STRATEGIC_AGENT",
                Task.title == title,
                Task.created_at >= dedup_since,
            )
            .order_by(Task.created_at.asc())
        )
        for candidate in (await session.execute(dupe_stmt)).scalars().all():
            cctx = candidate.context or {}
            if (
                cctx.get("spawned_by") == "strategic_direct"
                and cctx.get("parent_strategic_task_id") == str(task.id)
                and (candidate.first_payload or {}).get("type") == spawn_type
            ):
                logger.info(
                    "[STRATEGIC API] spawn_task idempotency: returning existing "
                    "%s for duplicate spawn from strategic review %s "
                    "(type=%s, title=%r)",
                    candidate.id,
                    task.id,
                    spawn_type,
                    title,
                )
                return _resp(
                    {
                        "status": "ok",
                        "op": "spawn_task",
                        "task_id": str(candidate.id),
                        "type": spawn_type,
                        "priority": candidate.priority,
                        "deduplicated": True,
                    }
                )

        new_task_ctx: dict = {
            "spawned_by": "strategic_direct",
            "parent_strategic_task_id": str(task.id),
        }
        if direction_id:
            new_task_ctx["direction_id"] = direction_id

        new_task = Task(
            project_id=project.id,
            title=title,
            description=prompt[:500],
            payloads=[spawn_payload],
            context=new_task_ctx,
            status=TaskStatus.QUEUED,
            priority=100,  # ahead of default (50) — course-correction runs first
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            created_by="STRATEGIC_AGENT",
        )
        session.add(new_task)
        await session.flush()

        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_tasks_spawned": ctx.get("_strategic_tasks_spawned", 0) + 1,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Spawned {spawn_type} task {new_task.id} "
            f"(priority=100) from strategic review {task.id} for project {project.id}"
        )
        return _resp(
            {
                "status": "ok",
                "op": "spawn_task",
                "task_id": str(new_task.id),
                "type": spawn_type,
                "priority": 100,
            }
        )

    elif payload.op == "set_problem_model":
        # Full-model replacement. Validated and merged via apply_agent_update:
        # bumps revision, stamps last_updated, auto-archives closed/abandoned
        # gaps, preserves existing archived_gaps + revision_log, appends a
        # revision_log entry with the structured diff.
        from pydantic import ValidationError as _PmValidationError

        from app import strategic_cycle as _strategic_cycle
        from app.problem_model import apply_agent_update, operator_owned_subset
        from app.strategic_completion import (
            QUALIFY_REQUEST_CONTEXT_KEY,
            VERSION_PHASE_DECISION_CONTEXT_KEY,
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
        try:
            new_pm_raw, pm_diff = apply_agent_update(
                current_raw=project.problem_model,
                proposed_raw=pm_data,
                now_iso=_utcnow().isoformat(),
                baseline_raw=baseline_pm,
            )
        except _PmValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Invalid problem_model: {ve}"
            ) from None
        project.problem_model = new_pm_raw
        flag_modified(project, "problem_model")
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
        if version_phase_decision is not None:
            new_ctx[VERSION_PHASE_DECISION_CONTEXT_KEY] = version_phase_decision
        if qualify_request is not None:
            new_ctx[QUALIFY_REQUEST_CONTEXT_KEY] = qualify_request
        task.context = new_ctx
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await _emit_updated(STRATEGIC_KIND_PROBLEM_MODEL)
        logger.info(
            f"[STRATEGIC API] problem_model updated for project {project.id} "
            f"to revision {new_pm_raw.get('revision')} ({pm_diff or 'no diff'})"
        )
        if version_phase_decision is not None:
            logger.info(
                "[STRATEGIC API] version_phase_decision (phase=%s) routed to "
                "task.context for task %s — applied at strategic completion",
                version_phase_decision["phase"],
                task.id,
            )
        if qualify_request is not None:
            logger.info(
                "[STRATEGIC API] qualify_request routed to task.context for "
                "task %s — release qualifier runs at strategic completion if "
                "the open version is in stabilize",
                task.id,
            )
        return _resp(
            {
                "status": "ok",
                "op": "set_problem_model",
                "revision": new_pm_raw.get("revision"),
                "diff": pm_diff,
                "version_phase_decision_received": version_phase_decision is not None,
                "qualify_request_received": qualify_request is not None,
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
        await _emit_updated(STRATEGIC_KIND_RUNNING_UPDATE)
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

    elif payload.op == "pending_confirm":
        # Add a pending_user_confirmation item to the problem_model.
        # Scoped to external blockers only: credentials, access, major
        # pivots. Agents can ONLY create — users resolve via the UI.
        from pydantic import ValidationError as _PcValidationError

        from app.problem_model import (
            PendingConfirmation,
            parse_problem_model,
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
        current_model = parse_problem_model(project.problem_model)
        current_model.pending_user_confirmation.append(pending)
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

    else:
        raise HTTPException(status_code=400, detail=f"Unknown op: {payload.op}")
