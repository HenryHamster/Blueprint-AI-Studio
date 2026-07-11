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
  GET    /projects/{project_id}/tools/manifest             discover project-declared tools (member+)
  POST   /projects/{project_id}/tools/{tool_slug}/asset-token  mint capability JWT (member+)
  GET    /projects/{project_id}/tools/{tool_slug}/assets/{path:path}  serve tool assets (JWT auth)

The content-save path uses ``lock_project_or_429`` to serialise writes and an
optimistic ``base_revision``/409 guard that mirrors the design-doc revision
pattern in ``admin_projects.py``.

The asset serving endpoint uses a separate router WITHOUT the ``get_current_user``
dependency — it validates a short-lived capability JWT from the query string
so opaque-origin (``null``) iframes can load tool assets without cookies.
"""

import json as _json
import logging
import mimetypes
import re
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import get_session
from app.models import Project, ProjectTool, _utcnow
from app.auth import get_current_user, AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
from app.admin_helpers import _get_user_project_with_role
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
    "notes": {
        "config_schema": {},
        "default_title": "Notes",
        "default_placement": "project_section",
        "label": "Notes",
        "description": "A persistent Markdown scratchpad for notes, hypotheses, and equations (LaTeX renders in preview).",
    },
}

# Default tool(s) installed by the seed endpoint, keyed by slug.
_SEED_TOOLS: Dict[str, Dict[str, Any]] = {
    "whiteboard": {
        "kind": "whiteboard",
        "title": "Whiteboard",
        "placement": "project_section",
    },
    "notes": {
        "kind": "notes",
        "title": "Notes",
        "placement": "project_section",
    },
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")

# ── Public asset router (NO get_current_user — iframe auth via JWT) ──────────

asset_router = APIRouter(
    prefix="/api/admin",
    tags=["admin-project-tool-assets"],
)


# ── Manifest discovery cache ─────────────────────────────────────────────────
#
# Project-declared tools live in ``.blueprint/tools.json`` in the project's
# connected GitHub repo.  We cache parse results in-memory with a short TTL so
# repeated requests don't hammer the GitHub API.  Cache failures (missing file,
# malformed JSON) too — they get a shorter 60s TTL to avoid re-fetching on
# every request while still picking up fixes quickly.

_MANIFEST_CACHE_TTL_OK = 300       # 5 minutes for a successful parse
_MANIFEST_CACHE_TTL_FAIL = 60      # 1 minute for a failure (missing/malformed)
_MANIFEST_CACHE_MAX = 500          # entries
_manifest_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _prune_manifest_cache() -> None:
    """Evict expired entries so the cache doesn't grow unbounded."""
    now = _time.monotonic()
    stale = [k for k, (exp, _result) in _manifest_cache.items() if now >= exp]
    for k in stale:
        _manifest_cache.pop(k, None)
    # If still over the cap after pruning stale, evict oldest by expiry.
    if len(_manifest_cache) > _MANIFEST_CACHE_MAX:
        sorted_keys = sorted(_manifest_cache, key=lambda k: _manifest_cache[k][0])
        for k in sorted_keys[: len(_manifest_cache) - _MANIFEST_CACHE_MAX]:
            _manifest_cache.pop(k, None)


# ── Manifest schema ──────────────────────────────────────────────────────────

# Required fields in each tool entry.
_MANIFEST_TOOL_REQUIRED = {"slug", "title", "entry"}

# The .blueprint/tools.json keys we accept.  Anything outside this set is
# silently ignored so the manifest format can grow without breaking readers.
_MANIFEST_TOOL_KNOWN_KEYS = {
    "slug", "title", "description", "placement", "entry",
}

# Valid placements (must match admin-ui rendering buckets).
_VALID_PLACEMENTS = {"project_section", "toolbar", "sidebar"}

# ── Skill schema (same manifest, parallel array) ─────────────────────────────
#
# Skills are declared in the same ``.blueprint/tools.json`` manifest as an
# optional ``skills`` array alongside the existing ``tools`` array. Each skill
# entry describes a reusable agent procedure — its slug, title, shell command,
# and an optional description.

# Required fields in each skill entry.
_MANIFEST_SKILL_REQUIRED = {"slug", "title", "command"}

# The .blueprint/tools.json skill keys we accept.
_MANIFEST_SKILL_KNOWN_KEYS = {
    "slug", "title", "command", "description",
}

_SLUG_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")

# ── Compute image schema ─────────────────────────────────────────────────────
#
# A ``compute`` section in ``.blueprint/tools.json`` declares the baked
# container image to use for compute experiments in this project.  Example:
#
#   {
#     "compute": {
#       "image": { "name": "ghcr.io/org/my-project:v1.2@sha256:abc..." }
#     }
#   }
#
# The ``name`` field is the only required key; it is the OCI image reference
# (registry/repo:tag or :tag@digest).

_MANIFEST_COMPUTE_IMAGE_REQUIRED = {"name"}


def _validate_manifest_compute_image(spec: Any) -> str | None:
    """Validate the ``compute.image`` object from the manifest.

    Returns an error message string on failure, or ``None`` on success.
    """
    if not isinstance(spec, dict):
        return f"compute.image: must be an object, got {type(spec).__name__}"
    missing = _MANIFEST_COMPUTE_IMAGE_REQUIRED - set(spec.keys())
    if missing:
        return f"compute.image: missing required fields: {sorted(missing)}"
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        return "compute.image: name must be a non-empty string"
    return None


def _normalize_manifest_compute_image(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a validated ``compute.image`` entry into the response shape."""
    return {"name": spec["name"].strip()}


def _validate_manifest_tool(entry: Dict[str, Any], index: int) -> Optional[str]:
    """Validate a single tool entry from the manifest.  Returns an error
    message string on failure, or ``None`` on success."""
    if not isinstance(entry, dict):
        return f"tools[{index}]: must be an object, got {type(entry).__name__}"
    missing = _MANIFEST_TOOL_REQUIRED - set(entry.keys())
    if missing:
        return f"tools[{index}]: missing required fields: {sorted(missing)}"
    slug = entry.get("slug")
    if not isinstance(slug, str) or not _SLUG_VALUE_RE.match(slug):
        return f"tools[{index}]: slug must match {_SLUG_VALUE_RE.pattern}"
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        return f"tools[{index}]: title must be a non-empty string"
    entry_path = entry.get("entry")
    if not isinstance(entry_path, str) or not entry_path.strip():
        return f"tools[{index}]: entry must be a non-empty path string"
    # Reject paths containing ".." (traversal) or starting with "/" (absolute).
    if ".." in entry_path or entry_path.startswith("/"):
        return f"tools[{index}]: entry path must be relative and must not contain '..'"
    placement = entry.get("placement", "project_section")
    if placement not in _VALID_PLACEMENTS:
        return f"tools[{index}]: placement must be one of {sorted(_VALID_PLACEMENTS)}"
    # Extra unknown keys: silently ignored.
    return None


def _normalize_manifest_tool(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a validated tool entry into the response shape."""
    return {
        "slug": entry["slug"],
        "title": entry["title"].strip(),
        "description": (entry.get("description") or "").strip() if isinstance(entry.get("description"), str) else "",
        "placement": entry.get("placement", "project_section"),
        "entry": entry["entry"].strip(),
        "kind": "project",
        "source": "project",
    }


def _validate_manifest_skill(entry: Dict[str, Any], index: int) -> Optional[str]:
    """Validate a single skill entry from the manifest.  Returns an error
    message string on failure, or ``None`` on success."""
    if not isinstance(entry, dict):
        return f"skills[{index}]: must be an object, got {type(entry).__name__}"
    missing = _MANIFEST_SKILL_REQUIRED - set(entry.keys())
    if missing:
        return f"skills[{index}]: missing required fields: {sorted(missing)}"
    slug = entry.get("slug")
    if not isinstance(slug, str) or not _SLUG_VALUE_RE.match(slug):
        return f"skills[{index}]: slug must match {_SLUG_VALUE_RE.pattern}"
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        return f"skills[{index}]: title must be a non-empty string"
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return f"skills[{index}]: command must be a non-empty string"
    # Extra unknown keys: silently ignored.
    return None


def _normalize_manifest_skill(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a validated skill entry into the response shape."""
    result: Dict[str, Any] = {
        "slug": entry["slug"],
        "title": entry["title"].strip(),
        "command": entry["command"].strip(),
    }
    description = entry.get("description")
    if isinstance(description, str) and description.strip():
        result["description"] = description.strip()
    return result


async def _discover_manifest_tools(
    project: Project,
) -> Dict[str, Any]:
    """Fetch and parse ``.blueprint/tools.json`` from the project's connected repo.

    Returns a dict ``{tools: [...], skills: [...], compute: dict | None, error: str | None}``.
    ``compute`` is an optional dict with an ``image`` key when the manifest declares
    a compute section.  ``error`` is set only for a structural problem with the manifest
    (malformed JSON, schema violation) — a missing repo or missing manifest file
    returns empty lists with no error.
    """
    owner = (project.github_owner or "").strip()
    repo_name = (project.github_repo or "").strip()
    project_id_str = str(project.id)

    if not owner or not repo_name:
        return {"tools": [], "skills": [], "compute": {"image": None}, "error": None}

    cache_key = f"{owner}/{repo_name}"

    # Check manifest cache.
    _prune_manifest_cache()
    cached = _manifest_cache.get(cache_key)
    if cached is not None and _time.monotonic() < cached[0]:
        return cached[1]

    try:
        from app.github_auth import get_repo_installation_read_token
        from app.github_refs import get_repo_default_branch
        from app.github import get_file_text_at_ref

        token = await get_repo_installation_read_token(owner, repo_name)
        if not token:
            logger.warning(
                "[project-tools] No read token for %s/%s — cannot fetch manifest",
                owner, repo_name,
            )
            result = {"tools": [], "skills": [], "compute": {"image": None}, "error": None}
            _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_FAIL, result)
            return result

        default_branch = await get_repo_default_branch(token, owner, repo_name)
        manifest_text = await get_file_text_at_ref(
            ".blueprint/tools.json",
            token=token,
            owner=owner,
            repo=repo_name,
            ref=default_branch,
            project_id=project_id_str,
        )

        if manifest_text is None:
            # File doesn't exist — not an error, just no project-declared tools.
            result = {"tools": [], "skills": [], "compute": {"image": None}, "error": None}
            _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_OK, result)
            return result

        try:
            manifest = _json.loads(manifest_text)
        except _json.JSONDecodeError as e:
            error_msg = f"Invalid JSON in .blueprint/tools.json: {e}"
            logger.warning("[project-tools] %s for %s", error_msg, cache_key)
            result = {"tools": [], "skills": [], "compute": {"image": None}, "error": error_msg}
            _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_FAIL, result)
            return result

        if not isinstance(manifest, dict):
            error_msg = ".blueprint/tools.json must be a JSON object with a 'tools' array"
            result = {"tools": [], "skills": [], "compute": {"image": None}, "error": error_msg}
            _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_FAIL, result)
            return result

        raw_tools = manifest.get("tools")
        if raw_tools is None:
            raw_tools = []
        elif not isinstance(raw_tools, list):
            error_msg = ".blueprint/tools.json 'tools' field must be an array"
            result = {"tools": [], "skills": [], "compute": {"image": None}, "error": error_msg}
            _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_FAIL, result)
            return result

        tools: List[Dict[str, Any]] = []
        errors: List[str] = []
        seen_slugs: set[str] = set()
        for i, entry in enumerate(raw_tools):
            err = _validate_manifest_tool(entry, i)
            if err:
                errors.append(err)
                continue
            slug = entry["slug"]
            if slug in seen_slugs:
                errors.append(f"tools[{i}]: duplicate slug '{slug}'")
                continue
            seen_slugs.add(slug)
            tools.append(_normalize_manifest_tool(entry))

        # Parse optional skills array alongside tools.
        raw_skills = manifest.get("skills")
        skills: List[Dict[str, Any]] = []
        if isinstance(raw_skills, list):
            seen_skill_slugs: set[str] = set()
            for i, entry in enumerate(raw_skills):
                err = _validate_manifest_skill(entry, i)
                if err:
                    errors.append(err)
                    continue
                slug = entry["slug"]
                if slug in seen_skill_slugs:
                    errors.append(f"skills[{i}]: duplicate slug '{slug}'")
                    continue
                seen_skill_slugs.add(slug)
                skills.append(_normalize_manifest_skill(entry))

        # Parse optional compute section alongside tools and skills.
        compute_image: Dict[str, Any] | None = None
        raw_compute = manifest.get("compute")
        if isinstance(raw_compute, dict):
            raw_compute_image = raw_compute.get("image")
            if raw_compute_image is not None:
                err = _validate_manifest_compute_image(raw_compute_image)
                if err:
                    errors.append(err)
                else:
                    compute_image = _normalize_manifest_compute_image(raw_compute_image)

        if errors:
            error_msg = "Manifest validation errors: " + "; ".join(errors)
            logger.warning("[project-tools] %s for %s", error_msg, cache_key)
            result = {"tools": tools, "skills": skills, "compute": {"image": compute_image}, "error": error_msg}
            _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_FAIL, result)
            return result

        result = {"tools": tools, "skills": skills, "compute": {"image": compute_image}, "error": None}
        _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_OK, result)
        return result

    except Exception as e:  # noqa: BLE001 — manifest fetch must never crash the endpoint
        logger.warning(
            "[project-tools] Manifest fetch error for %s: %s", cache_key, e,
        )
        result = {"tools": [], "skills": [], "compute": {"image": None}, "error": f"Failed to fetch manifest: {e}"}
        _manifest_cache[cache_key] = (_time.monotonic() + _MANIFEST_CACHE_TTL_FAIL, result)
        return result


# ── Content-Type detection ───────────────────────────────────────────────────

# Extension → MIME type mapping for common web asset types.  Falls back to
# mimetypes.guess_type for anything not in the map, then to octet-stream.
_ASSET_MIME_MAP: Dict[str, str] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

# Extensions that are safe to serve as text (via get_file_text_at_ref) — all
# others go through the binary-safe get_file_bytes_at_ref.
_TEXT_EXTENSIONS = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".svg", ".txt", ".md", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"}


def _content_type_for_path(path: str) -> str:
    """Return the Content-Type for *path*, falling back to octet-stream."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    dotted = f".{ext}" if ext else ""
    if dotted in _ASSET_MIME_MAP:
        return _ASSET_MIME_MAP[dotted]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


# ── Asset token creation ─────────────────────────────────────────────────────

ASSET_TOKEN_TTL_MINUTES = 5


def _create_tool_asset_token(project_id: str, tool_slug: str) -> str:
    """Mint a short-lived capability JWT for tool-asset access.

    The token carries ``scope: "tool-asset"`` as an explicit discriminator so
    the asset endpoint can reject agent tokens (which lack this scope) and vice
    versa.  Signed with ``AGENT_JWT_SECRET`` (HS256) — the same key as agent
    tokens, but the scope claim prevents cross-use.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ASSET_TOKEN_TTL_MINUTES)
    to_encode = {
        "scope": "tool-asset",
        "project_id": project_id,
        "tool_slug": tool_slug,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM)


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


async def _get_tool_for_member(
    session: AsyncSession, project_id: uuid.UUID, tool_id: uuid.UUID, user_id: str
) -> ProjectTool:
    """Authorize a user as project member then fetch the tool, or raise.

    Combines the member-level auth check with the scoped tool lookup so a
    future content-path endpoint cannot accidentally skip membership
    enforcement.
    """
    await _get_user_project_with_role(project_id, user_id, session, "member")
    return await _get_project_tool(session, project_id, tool_id)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/tools")
async def list_project_tools(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List a project's registered tools (member+). Lean projection, no content."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    # Total count so the frontend can render pagination controls.
    count_result = await session.execute(
        select(func.count()).select_from(ProjectTool).where(
            ProjectTool.project_id == project_id
        )
    )
    total_count = count_result.scalar_one()
    rows = (
        await session.execute(
            select(ProjectTool)
            .where(ProjectTool.project_id == project_id)
            .order_by(ProjectTool.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    next_offset = offset + len(rows) if offset + len(rows) < total_count else None
    return {
        "tools": [_tool_summary(t) for t in rows],
        "total_count": total_count,
        "next_offset": next_offset,
    }


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
    tool = await _get_tool_for_member(
        session, project_id, tool_id, current_user["user_id"]
    )
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


# ── Manifest discovery endpoint ──────────────────────────────────────────────


@router.get("/projects/{project_id}/tools/manifest")
async def discover_project_tool_manifest(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Discover tools declared in the project's ``.blueprint/tools.json``.

    Returns a JSON object with a ``tools`` array, a ``skills`` array, an optional
    ``compute`` dict (with an ``image`` key when the project declares a compute
    image), and an optional ``error`` string.  Each tool entry carries ``slug``,
    ``title``, ``description``, ``placement``, ``entry``, ``kind`` (always
    ``"project"``), and ``source`` (always ``"project"``).  An empty list means
    no project-declared tools.

    Graceful handling: no connected repo → empty list; no manifest file →
    empty list; malformed manifest → structured error in the response body.
    Requires member role on the project.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    result = await _discover_manifest_tools(project)
    return result


# ── Capability-token minting endpoint ────────────────────────────────────────


@router.post("/projects/{project_id}/tools/{tool_slug}/asset-token")
async def mint_tool_asset_token(
    project_id: uuid.UUID,
    tool_slug: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Mint a short-lived capability JWT for accessing a project tool's assets.

    The token carries ``scope: "tool-asset"``, ``project_id``, ``tool_slug``,
    ``exp`` (5-minute TTL), and ``iat``.  Signed with the same HS256 secret as
    agent tokens but scoped so it cannot be used as an agent token.

    Returns ``{token, expires_at}`` (ISO timestamp).  Rejects unknown tool
    slugs with 404 — the slug must be present in the project's
    ``.blueprint/tools.json`` manifest.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    # Resolve the manifest to verify the slug exists.
    result = await _discover_manifest_tools(project)
    if result.get("tools") is None or not any(
        t.get("slug") == tool_slug for t in result.get("tools", [])
    ):
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_slug}' not found in project manifest",
        )
    token = _create_tool_asset_token(str(project_id), tool_slug)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ASSET_TOKEN_TTL_MINUTES)
    return {"token": token, "expires_at": expires_at.isoformat()}


# ── Asset serving endpoint (public router — JWT auth, no cookies) ────────────


def _verify_tool_asset_token(token: str) -> dict:
    """Verify a tool-asset capability JWT.

    Returns the decoded payload on success.  Raises HTTPException(401) on
    invalid/expired tokens.  Also rejects tokens whose ``scope`` is not
    ``"tool-asset"`` — agent tokens cannot access this endpoint.
    """
    try:
        payload = jwt.decode(token, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Asset token expired") from None
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid asset token") from None
    if payload.get("scope") != "tool-asset":
        raise HTTPException(status_code=401, detail="Token scope is not tool-asset")
    return payload


@asset_router.get("/projects/{project_id}/tools/{tool_slug}/assets/{path:path}")
async def serve_tool_asset(
    project_id: uuid.UUID,
    tool_slug: str,
    path: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: str = Query(..., description="Capability JWT for tool-asset access"),
):
    """Serve a static asset for a project-declared tool.

    Authenticated via a short-lived ``token`` query parameter (not cookies /
    Firebase — opaque-origin iframes cannot send cookies).  The token must
    carry ``scope: "tool-asset"``, the correct ``project_id``, and the correct
    ``tool_slug``.

    Files are read from ``.blueprint/tools/{tool_slug}/{path}`` in the
    project's connected GitHub repo default branch.  Content-Type is detected
    from the file extension.  Security: path traversal (``..``) and absolute
    paths are rejected.
    """
    # 1. Validate the capability JWT.
    payload = _verify_tool_asset_token(token)
    if payload.get("project_id") != str(project_id):
        raise HTTPException(status_code=401, detail="Token project_id mismatch")
    if payload.get("tool_slug") != tool_slug:
        raise HTTPException(status_code=401, detail="Token tool_slug mismatch")

    # 2. Security: reject path traversal and absolute paths.
    if ".." in path or path.startswith("/") or path.startswith("\\"):
        raise HTTPException(status_code=400, detail="Invalid asset path")

    # 3. Look up the project to get GitHub repo info.
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    owner = (project.github_owner or "").strip()
    repo_name = (project.github_repo or "").strip()
    if not owner or not repo_name:
        raise HTTPException(status_code=404, detail="Project has no connected repo")

    # 4. Fetch the file from GitHub.
    repo_file_path = f".blueprint/tools/{tool_slug}/{path}"

    from app.github_auth import get_repo_installation_read_token
    from app.github_refs import get_repo_default_branch

    gh_token = await get_repo_installation_read_token(owner, repo_name)
    if not gh_token:
        raise HTTPException(
            status_code=502,
            detail="Unable to obtain GitHub token for repo access",
        )

    default_branch = await get_repo_default_branch(gh_token, owner, repo_name)

    content_type = _content_type_for_path(path)
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    is_text = f".{ext}" in _TEXT_EXTENSIONS if ext else True

    try:
        if is_text:
            from app.github import get_file_text_at_ref

            text = await get_file_text_at_ref(
                repo_file_path,
                token=gh_token,
                owner=owner,
                repo=repo_name,
                ref=default_branch,
                project_id=str(project_id),
            )
            if text is None:
                raise HTTPException(status_code=404, detail="File not found")
            body: bytes = text.encode("utf-8")
        else:
            from app.github import get_file_bytes_at_ref

            raw = await get_file_bytes_at_ref(
                repo_file_path,
                token=gh_token,
                owner=owner,
                repo=repo_name,
                ref=default_branch,
                project_id=str(project_id),
            )
            if raw is None:
                raise HTTPException(status_code=404, detail="File not found")
            body = raw
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(
            "[project-tools] Asset fetch error %s/%s/%s: %s",
            owner, repo_name, repo_file_path, e,
        )
        raise HTTPException(status_code=502, detail=f"Failed to fetch asset: {e}")

    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=300",
        },
    )
