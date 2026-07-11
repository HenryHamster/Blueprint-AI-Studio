"""Per-project custom UI tools registry endpoints.

Backs the per-project custom-tools framework (design doc 4d6060ba). A project
registers ``ProjectTool`` entries (e.g. a whiteboard/canvas panel) that the
admin-ui conditionally renders in that project's workspace only. Tools never
leak across projects: every handler resolves the project (and the tool row
scoped to it) through ``_get_user_project_with_role`` before touching data.

Endpoints (all under ``/api/admin``):
  GET    /projects/{project_id}/tools                      list (member+)
  POST   /projects/{project_id}/tools                      register (owner)
  PATCH  /projects/{project_id}/tools/{tool_id}            update metadata (owner)
  DELETE /projects/{project_id}/tools/{tool_id}            deregister (owner)
  GET    /projects/{project_id}/tools/{tool_id}/content    fetch content (member+)
  PUT    /projects/{project_id}/tools/{tool_id}/content    save content (member+)
  POST   /projects/{project_id}/tools/seed                 seed defaults (owner)

The content-save path uses ``lock_project_or_429`` to serialise writes and an
optimistic ``base_revision``/409 guard that mirrors the design-doc revision
pattern in ``admin_projects.py``.
"""

import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import get_session
from app.models import ProjectTool, _utcnow
from app.auth import get_current_user
from app.admin import _get_user_project_with_role
from app.task_locking import lock_project_or_429

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-project-tools"],
    dependencies=[Depends(get_current_user)],
)


# ── Tool-kind catalog ─────────────────────────────────────────────────────────
#
# TOOL_KINDS is the canonical Python view of the valid tool kinds. Its keys MUST
# match ``app/tool_kinds_manifest.json`` exactly — a parity test in
# ``testing/test_project_tools.py`` enforces this drift-guard so no kind can be
# added in code without also being declared in the checked-in manifest, and vice
# versa. Each entry describes the kind's config schema plus registration
# defaults.
TOOL_KINDS: Dict[str, Dict[str, Any]] = {
    "whiteboard": {
        "config_schema": {},
        "default_title": "Whiteboard",
        "default_placement": "project_section",
        "label": "Whiteboard",
        "description": "A collaborative canvas for sketching diagrams and ideas.",
    },
}

# Default tool(s) installed by the seed endpoint, keyed by slug.
_SEED_TOOLS: Dict[str, Dict[str, Any]] = {
    "whiteboard": {
        "kind": "whiteboard",
        "title": "Whiteboard",
        "placement": "project_section",
    },
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")

# ── Tool-kind endpoint (dynamic discovery for the frontend) ───────────────────

@router.get("/tool-kinds")
async def list_tool_kinds():
    """List available tool kinds with metadata for dynamic UI rendering.

    Returns a bare array matching the TypeScript ``ToolKindConfig`` interface so
    that ``fetchToolKinds`` in the admin-ui can populate registration forms and
    tool-host configuration panels without hardcoding kind metadata on the client.
    """
    result: List[Dict[str, Any]] = []
    for kind, spec in sorted(TOOL_KINDS.items()):
        entry: Dict[str, Any] = {
            "kind": kind,
            "label": spec.get("label", spec.get("default_title", kind)),
            "description": spec.get("description", ""),
        }
        icon = spec.get("icon")
        if icon is not None:
            entry["icon"] = icon
        entry["config_schema"] = spec.get("config_schema", {})
        result.append(entry)
    return result


# ── Pydantic request/response models ──────────────────────────────────────────


class _ToolCreate(BaseModel):
    kind: str
    slug: Optional[str] = None
    title: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    placement: Optional[str] = None


class _ToolUpdate(BaseModel):
    title: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
    placement: Optional[str] = None


class _ToolContentSave(BaseModel):
    content: Dict[str, Any] = Field(default_factory=dict)
    base_revision: int


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_uuid(raw: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {field} format")



def _tool_summary(tool: ProjectTool) -> Dict[str, Any]:
    """Lean list projection — deliberately omits the (potentially large) content."""
    return {
        "id": str(tool.id),
        "kind": tool.kind,
        "slug": tool.slug,
        "title": tool.title,
        "enabled": tool.enabled,
        "placement": tool.placement,
        "content_revision": tool.content_revision,
    }


def _validate_config(kind: str, config: Dict[str, Any], spec: Dict[str, Any]) -> None:
    """Validate tool config against the kind's ``config_schema``.

    Raises HTTPException(400) if validation fails.  An empty schema ``{}``
    (the default for kinds without constraints) accepts any config, so this
    is a no-op until a future kind declares a real schema.

    The subset of JSON Schema handled here covers the common patterns for
    tool-kind config: ``required`` field names and per-field ``type``
    constraints on top-level properties.  Complex combinators (``oneOf``,
    ``allOf``) and nested property schemas are not supported — if a future
    kind needs them, migrate to a real JSON Schema library.
    """
    schema = spec.get("config_schema", {})
    if not schema:
        return

    errors: List[str] = []

    # Check required top-level fields.
    for field in schema.get("required", []):
        if field not in config:
            errors.append(f"'{field}' is required")

    # Check type constraints on top-level properties.
    for field, prop_schema in schema.get("properties", {}).items():
        if field not in config:
            continue
        expected = prop_schema.get("type")
        if not expected:
            continue
        val = config[field]
        if expected == "string" and not isinstance(val, str):
            errors.append(f"'{field}' must be a string")
        elif expected == "integer" and not isinstance(val, int):
            errors.append(f"'{field}' must be an integer")
        elif expected == "boolean" and not isinstance(val, bool):
            errors.append(f"'{field}' must be a boolean")
        elif expected == "array" and not isinstance(val, list):
            errors.append(f"'{field}' must be an array")

    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid config for tool kind '{kind}': {'; '.join(errors)}",
        )


async def _get_project_tool(
    session: AsyncSession, project_id: uuid.UUID, tool_id: uuid.UUID
) -> ProjectTool:
    """Fetch a tool scoped to a project. 404 if it does not belong to it.

    The caller must have already authorised access to ``project_id``; scoping
    the lookup to that project ensures a tool from another project can never be
    read or mutated through a mismatched path.
    """
    tool = (
        await session.execute(
            select(ProjectTool).where(
                ProjectTool.id == tool_id,
                ProjectTool.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/tools")
async def list_project_tools(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List a project's registered tools (member+). Lean projection, no content."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    rows = (
        await session.execute(
            select(ProjectTool)
            .where(ProjectTool.project_id == project_id)
            .order_by(ProjectTool.created_at.asc())
        )
    ).scalars().all()
    return {"tools": [_tool_summary(t) for t in rows]}


@router.post("/projects/{project_id}/tools")
async def register_project_tool(
    project_id: uuid.UUID,
    payload: _ToolCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Register a new tool for a project (owner)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    kind = (payload.kind or "").strip()
    if kind not in TOOL_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tool kind '{kind}'. Must be one of: {sorted(TOOL_KINDS)}",
        )
    spec = TOOL_KINDS[kind]

    slug = (payload.slug or kind).strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="Invalid slug: must match ^[a-z0-9][a-z0-9\\-_]{0,63}$",
        )

    title = (payload.title or spec["default_title"]).strip()
    if not title:
        title = spec["default_title"]
    placement = (payload.placement or spec["default_placement"]).strip()
    if not placement:
        placement = spec["default_placement"]
    config = payload.config if isinstance(payload.config, dict) else {}
    _validate_config(kind, config, spec)

    # Enforce unique (project_id, slug) with a friendly 409 before hitting the
    # DB constraint, which would otherwise surface as a 500.
    existing = (
        await session.execute(
            select(ProjectTool.id).where(
                ProjectTool.project_id == project_id,
                ProjectTool.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A tool with slug '{slug}' already exists for this project",
        )

    tool = ProjectTool(
        project_id=project_id,
        kind=kind,
        slug=slug,
        title=title,
        placement=placement,
        config=config,
        created_by=current_user["user_id"],
    )
    session.add(tool)
    await session.commit()
    await session.refresh(tool)
    return _tool_summary(tool)


@router.patch("/projects/{project_id}/tools/{tool_id}")
async def update_project_tool(
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    payload: _ToolUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update a tool's metadata: enable/disable, rename, config/placement (owner)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    tool = await _get_project_tool(session, project_id, tool_id)

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title must not be empty")
        tool.title = title
    if payload.enabled is not None:
        tool.enabled = payload.enabled
    if payload.placement is not None:
        placement = payload.placement.strip()
        if not placement:
            raise HTTPException(status_code=400, detail="placement must not be empty")
        tool.placement = placement
    if payload.config is not None:
        if not isinstance(payload.config, dict):
            raise HTTPException(status_code=400, detail="config must be an object")
        _validate_config(tool.kind, payload.config, TOOL_KINDS.get(tool.kind, {}))
        tool.config = payload.config
        flag_modified(tool, "config")

    tool.updated_at = _utcnow()
    session.add(tool)
    await session.commit()
    await session.refresh(tool)
    return _tool_summary(tool)


@router.delete("/projects/{project_id}/tools/{tool_id}")
async def deregister_project_tool(
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Deregister (delete) a tool (owner). Idempotent-ish: 404 if already gone."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    tool = await _get_project_tool(session, project_id, tool_id)
    await session.delete(tool)
    await session.commit()
    return {"status": "deleted", "id": str(tool_id)}


@router.get("/projects/{project_id}/tools/{tool_id}/content")
async def get_project_tool_content(
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Fetch a tool's mutable content document plus its revision (member+)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    tool = await _get_project_tool(session, project_id, tool_id)
    return {"content": tool.content or {}, "content_revision": tool.content_revision}


@router.put("/projects/{project_id}/tools/{tool_id}/content")
async def save_project_tool_content(
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    payload: _ToolContentSave,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Save a tool's content with an optimistic-lock guard (member+).

    Serialises concurrent writers with ``lock_project_or_429`` (429 on
    contention, never a 30s-timeout 500 that would quarantine the instance) and
    rejects a stale ``base_revision`` with 409, mirroring the design-doc
    revision pattern. On success bumps ``content_revision`` by one.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Serialise writes on the owning project row before the read-modify-write.
    await lock_project_or_429(session, project_id)

    tool = await _get_project_tool(session, project_id, tool_id)

    if payload.base_revision != tool.content_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "Content was modified by another writer "
                f"(base_revision={payload.base_revision}, current={tool.content_revision}). "
                "Reload and retry."
            ),
        )

    tool.content = payload.content or {}
    flag_modified(tool, "content")
    tool.content_revision = tool.content_revision + 1
    tool.updated_at = _utcnow()
    session.add(tool)
    await session.commit()
    await session.refresh(tool)
    return {"content_revision": tool.content_revision}


@router.post("/projects/{project_id}/tools/seed")
async def seed_project_tools(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Idempotently install the default tool(s) for a project (owner).

    Returns ``{added, skipped}`` like ``admin_playbook_seed``: a slug already
    present is skipped, so repeated calls are safe.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    project = await lock_project_or_429(session, project_id)
    _ = project  # lock acquired; row identity unused beyond serialisation

    existing_slugs = set(
        (
            await session.execute(
                select(ProjectTool.slug).where(
                    ProjectTool.project_id == project_id
                )
            )
        ).scalars().all()
    )

    added: List[str] = []
    skipped: List[str] = []
    for slug, spec in _SEED_TOOLS.items():
        if slug in existing_slugs:
            skipped.append(slug)
            continue
        session.add(
            ProjectTool(
                project_id=project_id,
                kind=spec["kind"],
                slug=slug,
                title=spec["title"],
                placement=spec["placement"],
                created_by=current_user["user_id"],
            )
        )
        added.append(slug)

    if added:
        await session.commit()

    return {"status": "ok", "added": added, "skipped": skipped}
