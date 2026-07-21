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

import asyncio
import json as _json
import logging
import mimetypes
import os
import pathlib
import re
import time as _time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import get_session
from app.shared_state import limiter
from app.models import (
    ArtifactRef,
    ArtifactRefPromotionIntent,
    ComputeImageBuildStatus,
    Project,
    ProjectComputeImage,
    ProjectComputeImageBuild,
    ProjectMessageAttachmentUpload,
    ProjectTool,
    _utcnow,
)
from app.auth import (
    get_current_user,
    require_admin,
    AGENT_JWT_SECRET,
    AGENT_JWT_ALGORITHM,
    TOOL_ASSET_TOKEN_SCOPE,
)
from app.admin_helpers import _get_user_project_with_role
from app.task_locking import lock_project_or_429
from app import compute_backends as _cb
from app import compute_image_build as _cib
from app import reaper_sweeps as _rs

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


# ── Local-source fallback ─────────────────────────────────────────────────────
#
# When a project has no GitHub connection (or the GitHub fetch fails), the
# manifest and assets can be loaded from a directory on the local filesystem.
# Set BLUEPRINT_LOCAL_TOOLS_ROOT to a directory that mirrors the repo layout:
#   <root>/.blueprint/tools.json           — the manifest
#   <root>/.blueprint/tools/<slug>/<path>  — individual tool assets
#
# This is intentionally gated on the env var so production deployments without
# a configured local path are unaffected.

_BLUEPRINT_LOCAL_TOOLS_ROOT_ENV = "BLUEPRINT_LOCAL_TOOLS_ROOT"


def _get_local_tools_root() -> pathlib.Path | None:
    """Return the configured local tools root path, or None if not set."""
    raw = os.environ.get(_BLUEPRINT_LOCAL_TOOLS_ROOT_ENV, "").strip()
    if not raw:
        return None
    return pathlib.Path(raw)


def _read_local_manifest() -> str | None:
    """Read .blueprint/tools.json from the local tools root; returns None if absent."""
    root = _get_local_tools_root()
    if root is None:
        return None
    manifest_path = root / ".blueprint" / "tools.json"
    try:
        return manifest_path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def _read_local_asset(tool_slug: str, asset_path: str) -> bytes | None:
    """Read a tool asset from the local tools root; returns None if absent."""
    root = _get_local_tools_root()
    if root is None:
        return None
    # Resolve under the local root to prevent path traversal.
    asset_file = root / ".blueprint" / "tools" / tool_slug / asset_path
    try:
        resolved = asset_file.resolve()
        root_resolved = root.resolve()
        if not str(resolved).startswith(str(root_resolved)):
            return None
        return resolved.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError, ValueError):
        return None


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
#
# An optional ``build`` object declares how the per-project compute image is
# baked from the repo's own lockfiles (the PRODUCE side of the images-first
# direction).  A project that points at an externally pre-built image omits it.
#
#   {
#     "compute": {
#       "image": {
#         "name": "us-docker.pkg.dev/proj/compute-images/my-project:latest",
#         "build": {
#           "dockerfile": ".blueprint/Dockerfile.compute",
#           "parent": "python:3.11-slim",
#           "build_args": { "CUDA": "12.4" }
#         }
#       }
#     }
#   }

_MANIFEST_COMPUTE_IMAGE_REQUIRED = {"name"}

# Default Dockerfile path (relative to repo root) when ``build`` omits it.
_DEFAULT_COMPUTE_DOCKERFILE = ".blueprint/Dockerfile.compute"


def _validate_manifest_compute_image_build(spec: Any) -> str | None:
    """Validate the optional ``compute.image.build`` object.

    Returns an error message string on failure, or ``None`` on success.
    """
    if not isinstance(spec, dict):
        return f"compute.image.build: must be an object, got {type(spec).__name__}"
    dockerfile = spec.get("dockerfile")
    if dockerfile is not None and (
        not isinstance(dockerfile, str) or not dockerfile.strip()
    ):
        return "compute.image.build: dockerfile must be a non-empty string"
    if isinstance(dockerfile, str) and (
        ".." in dockerfile or dockerfile.startswith("/")
    ):
        return "compute.image.build: dockerfile must be a relative path without '..'"
    parent = spec.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        return "compute.image.build: parent must be a non-empty string"
    build_args = spec.get("build_args")
    if build_args is not None:
        if not isinstance(build_args, dict):
            return "compute.image.build: build_args must be an object of string->string"
        for k, v in build_args.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return "compute.image.build: build_args keys and values must be strings"
    deps = spec.get("deps")
    if deps is not None:
        if not isinstance(deps, list):
            return "compute.image.build: deps must be an array of strings"
        for i, entry in enumerate(deps):
            if not isinstance(entry, str) or not entry.strip():
                return f"compute.image.build: deps[{i}] must be a non-empty string"
            dep_path = entry.strip()
            # Validate using both POSIX and Windows path syntax because a
            # manifest can be authored on either platform while the service
            # runs on Linux.  A drive-qualified, rooted, UNC, or traversal path
            # must never be treated as repo-relative.
            if (
                dep_path.startswith(("/", "\\"))
                or re.match(r"^[A-Za-z]:", dep_path)
                or "\x00" in dep_path
                or any(part == ".." for part in re.split(r"[\\/]", dep_path))
            ):
                return (
                    f"compute.image.build: deps[{i}] must be a relative path "
                    "without '..'"
                )
    return None


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
    raw_build = spec.get("build")
    if raw_build is not None:
        build_err = _validate_manifest_compute_image_build(raw_build)
        if build_err:
            return build_err
    return None


def _normalize_manifest_compute_image(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a validated ``compute.image`` entry into the response shape."""
    result: Dict[str, Any] = {"name": spec["name"].strip(), "build": None}
    raw_build = spec.get("build")
    if isinstance(raw_build, dict):
        dockerfile = raw_build.get("dockerfile")
        parent = raw_build.get("parent")
        raw_args = raw_build.get("build_args")
        raw_deps = raw_build.get("deps")
        result["build"] = {
            "dockerfile": (
                dockerfile.strip()
                if isinstance(dockerfile, str) and dockerfile.strip()
                else _DEFAULT_COMPUTE_DOCKERFILE
            ),
            "parent": parent.strip() if isinstance(parent, str) else None,
            "build_args": (
                {str(k): str(v) for k, v in raw_args.items()}
                if isinstance(raw_args, dict)
                else {}
            ),
            "deps": (
                [str(d).strip() for d in raw_deps if isinstance(d, str)]
                if isinstance(raw_deps, list)
                else []
            ),
        }
    return result


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


def _parse_manifest_text(manifest_text: str, source_label: str) -> Dict[str, Any]:
    """Parse and validate a manifest JSON string into the standard result dict.

    *source_label* is used only in log messages (e.g. ``"local"`` or ``"github"``).
    """
    try:
        manifest = _json.loads(manifest_text)
    except _json.JSONDecodeError as e:
        error_msg = f"Invalid JSON in .blueprint/tools.json: {e}"
        logger.warning("[project-tools] %s (%s)", error_msg, source_label)
        return {"tools": [], "skills": [], "compute": {"image": None}, "error": error_msg}

    if not isinstance(manifest, dict):
        return {
            "tools": [],
            "skills": [],
            "compute": {"image": None},
            "error": ".blueprint/tools.json must be a JSON object with a 'tools' array",
        }

    raw_tools = manifest.get("tools")
    if raw_tools is None:
        raw_tools = []
    elif not isinstance(raw_tools, list):
        return {
            "tools": [],
            "skills": [],
            "compute": {"image": None},
            "error": ".blueprint/tools.json 'tools' field must be an array",
        }

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
        logger.warning("[project-tools] %s (%s)", error_msg, source_label)
        return {"tools": tools, "skills": skills, "compute": {"image": compute_image}, "error": error_msg}

    return {"tools": tools, "skills": skills, "compute": {"image": compute_image}, "error": None}


def _discover_manifest_tools_local() -> Dict[str, Any]:
    """Load and parse the manifest from the local filesystem fallback.

    Returns the same shape as ``_discover_manifest_tools``.  When no local root is
    configured or the manifest file is absent, returns empty lists with no error.
    """
    manifest_text = _read_local_manifest()
    if manifest_text is None:
        return {"tools": [], "skills": [], "compute": {"image": None}, "error": None}
    logger.debug("[project-tools] Loading manifest from local fallback (BLUEPRINT_LOCAL_TOOLS_ROOT)")
    return _parse_manifest_text(manifest_text, "local")


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
        return _discover_manifest_tools_local()

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
            # This is not equivalent to a repository with no manifest: callers
            # that make retention decisions must preserve existing protection
            # until a later read can establish whether an image is declared.
            result = {
                "tools": [],
                "skills": [],
                "compute": {"image": None},
                "error": "Repository read token unavailable; manifest could not be fetched",
            }
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

        result = _parse_manifest_text(manifest_text, cache_key)
        ttl = _MANIFEST_CACHE_TTL_FAIL if result.get("error") else _MANIFEST_CACHE_TTL_OK
        _manifest_cache[cache_key] = (_time.monotonic() + ttl, result)
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
        "scope": TOOL_ASSET_TOKEN_SCOPE,
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


class _ToolArtifactRefCreate(BaseModel):
    expected_revision: int = Field(ge=0)
    rendered_image: Dict[str, Any] | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


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
        # config is small metadata (kind-specific settings), not the mutable
        # content document — surfaced so the client Configure panel can prefill.
        "config": tool.config or {},
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

    explicit_slug = bool((payload.slug or "").strip())
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
    if placement not in _VALID_PLACEMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid placement '{placement}'. Must be one of: {sorted(_VALID_PLACEMENTS)}",
        )
    config = payload.config if isinstance(payload.config, dict) else {}
    _validate_config(kind, config, spec)

    # Load slugs already taken in this project so we can either 409 (explicit
    # slug) or auto-uniquify (omitted slug — lets an operator add a second tool
    # of the same kind without hitting a dead-end).
    taken = {
        s
        for (s,) in (
            await session.execute(
                select(ProjectTool.slug).where(
                    ProjectTool.project_id == project_id
                )
            )
        ).all()
    }

    if slug in taken:
        if explicit_slug:
            # Caller pinned this slug — surface the collision as a friendly 409
            # before the DB unique constraint turns it into a 500.
            raise HTTPException(
                status_code=409,
                detail=f"A tool with slug '{slug}' already exists for this project",
            )
        # Slug was derived from the kind; append the smallest free numeric
        # suffix so a second whiteboard/notes tool registers cleanly.
        base = slug
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        slug = f"{base}-{n}"

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
        if placement not in _VALID_PLACEMENTS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid placement '{placement}'. Must be one of: {sorted(_VALID_PLACEMENTS)}",
            )
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


async def _await_tracked_artifact_ref_promotion(task):
    """Bound the request wait without transferring cancellation to the WAL task."""

    return await asyncio.wait_for(asyncio.shield(task), timeout=25)


def _artifact_ref_pending_response(
    intent_id: uuid.UUID,
    receipt_id: uuid.UUID,
) -> JSONResponse:
    """Return the canonical durable-acceptance response for a pending copy."""

    return JSONResponse(
        status_code=202,
        headers={"Retry-After": "5"},
        content={
            "state": "pending",
            "promotion_intent_id": str(intent_id),
            "receipt_id": str(receipt_id),
            "retry_after_ms": 5_000,
        },
    )


@router.post(
    "/projects/{project_id}/tools/{tool_id}/artifact-refs",
    status_code=201,
)
@limiter.limit("30/minute")
async def create_project_tool_artifact_ref(
    request: Request,
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    payload: _ToolArtifactRefCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Freeze an exact Notes/Whiteboard revision for a Resident handoff."""

    del request
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.artifact_refs import (
        ArtifactRefConflict,
        artifact_ref_projection,
        create_tool_artifact_ref,
    )
    from app.storage import (
        StorageUriError,
        artifact_ref_render_uri,
    )
    from app.artifact_ref_promotion import (
        ArtifactRefPromotionBusy,
        expire_artifact_ref_promotion_claim,
        prepare_artifact_ref_promotion_intent,
        reconcile_one_artifact_ref_promotion,
        start_tracked_artifact_ref_promotion,
    )

    rendered_image = dict(payload.rendered_image or {}) or None
    promoted_uri: str | None = None
    candidate_promoted_uri: str | None = None
    promotion_intent_id: uuid.UUID | None = None
    promotion_claim_id: uuid.UUID | None = None
    if rendered_image is not None:
        preflight_tool = await session.get(ProjectTool, tool_id)
        if preflight_tool is None or preflight_tool.project_id != project_id:
            raise HTTPException(status_code=404, detail="Tool not found")
        if preflight_tool.kind != "whiteboard":
            raise HTTPException(
                status_code=400,
                detail="rendered images are only valid for Whiteboard handoff",
            )
        try:
            attachment_id = uuid.UUID(
                str(rendered_image.get("attachment_id") or rendered_image.get("id") or "")
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="whiteboard handoff requires a rendered image upload",
            ) from None
        receipt = await session.get(ProjectMessageAttachmentUpload, attachment_id)
        existing = (
            await session.execute(
                select(ArtifactRef).where(
                    ArtifactRef.project_id == project_id,
                    ArtifactRef.tool_id == tool_id,
                    ArtifactRef.revision == payload.expected_revision,
                )
            )
        ).scalar_one_or_none()
        receipt_identity_invalid = (
            receipt is None
            or receipt.project_id != project_id
            or receipt.actor_user_id != current_user["user_id"]
            or receipt.storage_uri
            != str(rendered_image.get("storage_uri") or "").strip()
        )
        receipt_already_used_elsewhere = bool(
            receipt is not None
            and (
                receipt.message_id is not None
                or (
                    receipt.artifact_ref_id is not None
                    and (existing is None or receipt.artifact_ref_id != existing.id)
                )
            )
        )
        receipt_unavailable_for_new_ref = bool(
            receipt is not None
            and existing is None
            and (
                receipt.consumed_at is not None
                or receipt.artifact_ref_id is not None
                or receipt.expires_at <= _utcnow()
            )
        )
        if (
            receipt_identity_invalid
            or receipt_already_used_elsewhere
            or receipt_unavailable_for_new_ref
        ):
            raise HTTPException(status_code=400, detail="Invalid rendered image upload receipt")
        if existing is None:
            source_storage_uri = receipt.storage_uri
            expected_size_bytes = receipt.expected_size_bytes
            candidate_promoted_uri = artifact_ref_render_uri(
                project_id,
                tool_id,
                payload.expected_revision,
                attachment_id,
            )
            try:
                promotion_claim = await prepare_artifact_ref_promotion_intent(
                    session,
                    project_id=project_id,
                    tool_id=tool_id,
                    revision=payload.expected_revision,
                    receipt_id=attachment_id,
                    actor_user_id=current_user["user_id"],
                    source_storage_uri=source_storage_uri,
                    candidate_storage_uri=candidate_promoted_uri,
                    expected_size_bytes=expected_size_bytes,
                    expected_sha256=str(rendered_image.get("checksum") or ""),
                    render_width=rendered_image.get("width") if isinstance(rendered_image.get("width"), int) else None,
                    render_height=rendered_image.get("height") if isinstance(rendered_image.get("height"), int) else None,
                )
                promotion_intent_id = promotion_claim.intent_id
                promotion_claim_id = promotion_claim.claim_id
            except ArtifactRefPromotionBusy as exc:
                await session.rollback()
                same_intent = (
                    await session.execute(
                        select(ArtifactRefPromotionIntent).where(
                            ArtifactRefPromotionIntent.project_id == project_id,
                            ArtifactRefPromotionIntent.tool_id == tool_id,
                            ArtifactRefPromotionIntent.revision == payload.expected_revision,
                            ArtifactRefPromotionIntent.receipt_id == attachment_id,
                            ArtifactRefPromotionIntent.state.notin_(["settled"]),
                        )
                    )
                ).scalar_one_or_none()
                if same_intent is None:
                    same_intent = (
                        await session.execute(
                            select(ArtifactRefPromotionIntent).where(
                                ArtifactRefPromotionIntent.project_id == project_id,
                                ArtifactRefPromotionIntent.tool_id == tool_id,
                                ArtifactRefPromotionIntent.revision
                                == payload.expected_revision,
                                ArtifactRefPromotionIntent.state != "settled",
                            )
                        )
                    ).scalar_one_or_none()
                if same_intent is None:
                    raise HTTPException(status_code=409, detail=str(exc)) from None
                # A live copy already has one tracked owner. Poll requests must
                # remain DB-only; starting GCS inspection here would create an
                # uncancellable worker thread on every retry. Promoted/owned
                # states are safe to reconcile because they need only DB work.
                if same_intent.state in {"promoted", "owned"}:
                    try:
                        async with asyncio.timeout(25):
                            reconciled = await reconcile_one_artifact_ref_promotion(
                                project_id=project_id,
                                tool_id=tool_id,
                                revision=payload.expected_revision,
                                receipt_id=same_intent.receipt_id,
                            )
                    except TimeoutError:
                        reconciled = None
                    if reconciled is not None:
                        return JSONResponse(
                            status_code=201,
                            content={
                                "state": "ready",
                                "artifact_ref": artifact_ref_projection(reconciled),
                            },
                        )
                # Keep the caller's durable receipt identity. This may be a
                # redundant upload waiting on another receipt's logical
                # promotion; replaying the same body adopts the immutable ref.
                return _artifact_ref_pending_response(
                    same_intent.id, attachment_id
                )
            except PermissionError as exc:
                await session.rollback()
                raise HTTPException(status_code=403, detail=str(exc)) from None
            except LookupError as exc:
                await session.rollback()
                raise HTTPException(status_code=404, detail=str(exc)) from None
            except ValueError as exc:
                await session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from None
            try:
                promotion_task = start_tracked_artifact_ref_promotion(
                    intent_id=promotion_intent_id,
                    claim_id=promotion_claim_id,
                    project_id=project_id,
                    tool_id=tool_id,
                    revision=payload.expected_revision,
                    receipt_id=attachment_id,
                    source_storage_uri=source_storage_uri,
                    expected_size_bytes=expected_size_bytes,
                    expected_sha256=str(rendered_image.get("checksum") or ""),
                )
                reconciled = await _await_tracked_artifact_ref_promotion(
                    promotion_task
                )
                if reconciled is None:
                    raise ArtifactRefPromotionBusy(
                        "Render promotion authority changed; reconciliation queued"
                    )
                return JSONResponse(
                    status_code=201,
                    content={
                        "state": "ready",
                        "artifact_ref": artifact_ref_projection(reconciled),
                    },
                )
            except StorageUriError:
                logger.warning(
                    "ArtifactRef promotion returned a storage error after WAL prepare",
                    extra={"promotion_intent_id": str(promotion_intent_id)},
                    exc_info=True,
                )
                return _artifact_ref_pending_response(
                    promotion_intent_id, attachment_id
                )
            except ArtifactRefPromotionBusy:
                await expire_artifact_ref_promotion_claim(
                    intent_id=promotion_intent_id,
                    claim_id=promotion_claim_id,
                    error="tracked promotion capacity unavailable",
                )
                return _artifact_ref_pending_response(
                    promotion_intent_id, attachment_id
                )
            except TimeoutError:
                return _artifact_ref_pending_response(
                    promotion_intent_id, attachment_id
                )
            except HTTPException:
                raise
            except Exception:
                logger.warning(
                    "ArtifactRef render promotion failed for project %s tool %s",
                    project_id,
                    tool_id,
                    extra={
                        "event": "artifact_ref_promotion_unknown",
                        "project_id": str(project_id),
                        "tool_id": str(tool_id),
                        "storage_uri": candidate_promoted_uri,
                    },
                    exc_info=True,
                )
                return _artifact_ref_pending_response(
                    promotion_intent_id, attachment_id
                )

    try:
        artifact_ref = await create_tool_artifact_ref(
            session,
            project_id=project_id,
            tool_id=tool_id,
            actor_user_id=current_user["user_id"],
            expected_revision=payload.expected_revision,
            rendered_image=rendered_image,
            promotion_intent_id=promotion_intent_id,
            promotion_claim_id=promotion_claim_id,
        )
    except ArtifactRefConflict as conflict:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "tool_revision_conflict",
                "expected_revision": conflict.expected_revision,
                "current_revision": conflict.current_revision,
                "action": "reload_or_rebase",
            },
        ) from None
    except LookupError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Tool not found") from None
    except PermissionError:
        await session.rollback()
        raise HTTPException(status_code=403, detail="Project member access changed") from None
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except HTTPException:
        await session.rollback()
        raise
    try:
        await session.commit()
    except Exception:
        # Commit outcome can be uncertain. Preserve the deterministic object so
        # a same-receipt retry can reconcile instead of deleting a durable ref.
        logger.warning(
            "ArtifactRef commit outcome unknown; promoted object retained for retry",
            extra={
                "event": "artifact_ref_commit_unknown",
                "project_id": str(project_id),
                "tool_id": str(tool_id),
                "storage_uri": promoted_uri,
            },
            exc_info=True,
        )
        raise
    await session.refresh(artifact_ref)
    return {"state": "ready", "artifact_ref": artifact_ref_projection(artifact_ref)}


@router.get("/projects/{project_id}/artifact-refs/{artifact_ref_id}")
async def get_project_tool_artifact_ref(
    project_id: uuid.UUID,
    artifact_ref_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Retrieve one immutable tool revision through project membership."""

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.artifact_refs import artifact_ref_projection, get_project_artifact_ref

    artifact_ref = await get_project_artifact_ref(
        session,
        project_id=project_id,
        artifact_ref_id=artifact_ref_id,
    )
    if artifact_ref is None:
        raise HTTPException(status_code=404, detail="ArtifactRef not found")
    return {
        "artifact_ref": artifact_ref_projection(artifact_ref),
        "content": artifact_ref.content_snapshot or {},
        "retrieval": artifact_ref.retrieval_metadata or {},
    }


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


async def _serve_tool_asset_impl(
    *,
    project_id: uuid.UUID,
    tool_slug: str,
    path: str,
    token: str,
    session: AsyncSession,
    add_referrer_policy: bool = False,
) -> Response:
    """Shared implementation for both asset-serving routes."""
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

    content_type = _content_type_for_path(path)

    if not owner or not repo_name:
        # No GitHub connection — try local filesystem fallback.
        local_body = _read_local_asset(tool_slug, path)
        if local_body is None:
            raise HTTPException(status_code=404, detail="Asset not found (no connected repo and no local fallback)")
        body: bytes = local_body
    else:
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
                body = text.encode("utf-8")
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

    headers = {
        "Cache-Control": "public, max-age=300",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox allow-scripts",
    }
    if add_referrer_policy and content_type == "text/html":
        headers["Referrer-Policy"] = "no-referrer"

    return Response(
        content=body,
        media_type=content_type,
        headers=headers,
    )


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
    if payload.get("scope") != TOOL_ASSET_TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="Token scope is not tool-asset")
    return payload


@asset_router.get("/projects/{project_id}/tools/{tool_slug}/t/{token}/assets/{path:path}")
async def serve_tool_asset_path_token(
    project_id: uuid.UUID,
    tool_slug: str,
    token: str,
    path: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Serve a static asset for a project-declared tool via a path-embedded token.

    The token lives in the URL path so that relative subresources (e.g.
    ``<script src="./app.js">``) resolve inside the token-prefix tree and
    authenticate automatically — the query-string token would be dropped on
    relative resolution, causing 401 for every subresource.

    The ``sandbox="allow-scripts"`` (no ``allow-same-origin``) iframe constraint
    makes cookies and custom headers impossible; path-embedding is the only
    mechanism that survives relative URL resolution inside an opaque-origin
    sandboxed iframe.

    Security mitigations for token-in-path leakage:
    - Short JWT TTL (same as the query-param route, ~5 min) limits the useful
      window if a token leaks through Referer or server logs.
    - ``Referrer-Policy: no-referrer`` is added to all HTML responses so the
      token is not forwarded in Referer headers when the page navigates to
      external resources.
    - Token is validated identically to the query-param route via
      ``_verify_tool_asset_token``.
    """
    return await _serve_tool_asset_impl(
        project_id=project_id,
        tool_slug=tool_slug,
        path=path,
        token=token,
        session=session,
        add_referrer_policy=True,
    )


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
    return await _serve_tool_asset_impl(
        project_id=project_id,
        tool_slug=tool_slug,
        path=path,
        token=token,
        session=session,
    )


# ── Compute-image build trigger endpoint ─────────────────────────────────────
#
# A compute-image build is a `docker build` + `docker push` of a REAL project
# image — a CUDA base plus a torch wheel is comfortably multi-GB and can run for
# the better part of an hour. It executes as a subprocess ON THE ORCHESTRATOR
# HOST, so an unbounded fan-out of these (one per member per click) is a
# self-inflicted DoS on the whole API process, not just on the caller's request.
#
# Two bounds, mirroring the semaphore pattern compute.py already uses to cap
# concurrent GCP provisions (`_gcp_provision_semaphore`):
#
#   1. CONCURRENCY — at most `_MAX_CONCURRENT_IMAGE_BUILDS` builds in flight
#      process-wide. The slot is taken NON-BLOCKING: a request that would have
#      to queue is rejected with 429 + Retry-After instead of parking an API
#      worker (and its DB connection) behind an hour-long docker build.
#   2. WALL CLOCK — a single build can never pin a worker indefinitely. The
#      inner subprocess timeouts (3600s build / 1800s push) can stack, so an
#      outer deadline bounds the whole pipeline and returns 504.
#
# Note the layer-scan inside the pipeline no longer buffers the `docker save`
# archive in memory (see compute_image_build.scan_image_layers_for_credentials);
# that was the OOM. These bounds cap the remaining blast radius: CPU, disk, and
# API workers.
_MAX_CONCURRENT_IMAGE_BUILDS = max(
    1, int(os.getenv("MAX_CONCURRENT_COMPUTE_IMAGE_BUILDS", "1"))
)
_compute_image_build_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_IMAGE_BUILDS)

# Outer deadline for the whole clone → hash → scan → build → push pipeline.
_COMPUTE_IMAGE_BUILD_TIMEOUT_SECONDS = int(
    os.getenv("COMPUTE_IMAGE_BUILD_TIMEOUT_SECONDS", "5400")  # 90 min
)

# Advertised to a rejected caller so it retries on a sane cadence rather than
# hot-looping against a saturated builder.
_COMPUTE_IMAGE_BUILD_RETRY_AFTER_SECONDS = 120

# After the deadline fires we ABORT the build (SIGKILL its docker children) and
# wait this long for the pipeline to unwind — that unwind is what removes the
# clone's temp dir and what releases the concurrency slot. Killing the children
# makes the blocking call return in milliseconds, so this window is slack, not a
# budget.
_COMPUTE_IMAGE_BUILD_ABORT_GRACE_SECONDS = 30


def _release_image_build_slot(task: "asyncio.Task") -> None:
    """Release the concurrency slot when the BUILD TASK ends — not when the request does.

    The slot belongs to the *build*, not to the HTTP request that started it. The
    old code released it in the request's ``finally``, so a 504 (or a client
    disconnect) handed the slot to the next caller while the abandoned docker
    build was still running on the host: the bound stopped bounding exactly when
    it mattered. Tying the release to task completion means the slot is held for
    the build's true lifetime, and the deadline path below is what makes that
    lifetime actually end.
    """
    _compute_image_build_semaphore.release()


async def _abort_and_unwind_image_build(
    control: "_cib.BuildControl", task: "asyncio.Task", project_id: uuid.UUID
) -> None:
    """Stop a deadline-exceeded build for real, and wait for its cleanup.

    ``asyncio.wait_for`` only cancels the coroutine that is *awaiting* the build;
    the ``docker build`` running in the ``asyncio.to_thread`` worker keeps going,
    holding the temp dir open while the pipeline's ``TemporaryDirectory`` context
    tears it down underneath it. Killing the docker children first makes the
    blocking call return, so the pipeline unwinds in the normal order (temp dir
    removed, slot released) with nothing still writing into it.
    """
    killed = control.abort()
    logger.warning(
        "[COMPUTE-IMAGE] aborting deadline-exceeded build for project=%s "
        "(signalled %d docker child process(es))",
        project_id,
        killed,
    )
    _, pending = await asyncio.wait({task}, timeout=_COMPUTE_IMAGE_BUILD_ABORT_GRACE_SECONDS)
    if not pending:
        _drain_task_exception(task)
        return
    # The kill did not land (no child running yet, or a wedged FS call). Cancel as
    # a last resort and give the unwind a moment; the slot is still released by the
    # done-callback whenever the task finally finishes.
    task.cancel()
    _, still_pending = await asyncio.wait({task}, timeout=5)
    if still_pending:
        logger.error(
            "[COMPUTE-IMAGE] build task for project=%s did not unwind after abort+cancel "
            "— its concurrency slot stays held until it does (fail-closed)",
            project_id,
        )
    else:
        _drain_task_exception(task)


def _drain_task_exception(task: "asyncio.Task") -> None:
    """Retrieve an aborted build's exception so asyncio does not log it as unhandled.

    The task died because WE killed it; that is expected, not an unhandled crash.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.info("[COMPUTE-IMAGE] aborted build unwound with %r", exc)


@dataclass
class _PreservedBuildRecord:
    """A ``BuildResult``-shaped view that keeps an already-stored digest.

    ``record_project_compute_image_build`` reads ``pinned_ref``/``tag``/``dep_hash``/
    ``built`` off whatever it is handed, and unconditionally writes them. Handing it
    a digest-less result for a dependency hash we have already pinned would NULL the
    stored digest (and flip ``built`` back to False), so a re-trigger with unchanged
    deps is exactly when the pin gets destroyed. This preserves what the DB already
    proved.
    """

    pinned_ref: str
    tag: str
    dep_hash: str
    built: bool
    already_present: bool = True


async def _preserve_existing_digest_pin(
    session: AsyncSession, project_id: uuid.UUID, result: Any
) -> Any:
    """Never let a digest-less build result overwrite a stored digest.

    An unchanged-deps re-trigger takes ``build_and_push``'s already-present fast
    path. That path now resolves the digest from the registry, but a registry that
    is briefly unreachable (or a tag-only push) still yields ``digest=None`` — and
    "None" there means UNKNOWN, not "no digest". Recording it would unpin the
    project's compute image, silently: the row keeps its dep_hash and its tag, so
    nothing looks wrong until a compute job resolves a mutable tag instead of the
    digest it was promised.
    """
    pinned_ref = getattr(result, "pinned_ref", None) or ""
    if _cb._parse_digest_from_pinned_ref(pinned_ref):
        return result  # carries a digest — nothing to protect

    dep_hash = getattr(result, "dep_hash", None) or ""
    if not dep_hash:
        return result

    existing = (
        await session.execute(
            select(ProjectComputeImage)
            .where(
                ProjectComputeImage.project_id == project_id,
                ProjectComputeImage.dep_hash == dep_hash,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None or not existing.digest:
        return result  # nothing stored to protect

    logger.warning(
        "[COMPUTE-IMAGE] build for project=%s dep_hash=%s resolved no digest; keeping "
        "the stored pin %s rather than overwriting it with NULL",
        project_id,
        dep_hash,
        existing.pinned_ref,
    )
    return _PreservedBuildRecord(
        pinned_ref=existing.pinned_ref,
        tag=existing.tag or getattr(result, "tag", "") or "",
        dep_hash=dep_hash,
        # An existing built row stays built: the image is in the registry, we just
        # could not re-read its digest this second.
        built=bool(existing.built or getattr(result, "built", False)),
        already_present=bool(getattr(result, "already_present", True)),
    )


@router.post("/projects/{project_id}/compute-image/build")
async def trigger_project_compute_image_build(
    project_id: uuid.UUID,
    dry_run: bool = Query(False, description="When true, skip the actual docker build/push"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Trigger a per-project compute-image build and persist the result.

    Discovers the project manifest, extracts the ``compute.image.build`` config,
    invokes ``compute_backends.build_project_compute_image``, and UPSERTs the
    resulting ``(dep_hash -> digest)`` mapping into ``ProjectComputeImage``.

    Returns the persisted row fields: ``pinned_ref``, ``digest``, ``dep_hash``,
    ``tag``, ``built``, ``already_present``.  Surfaces build failures (ValueError /
    RuntimeError from the pipeline) as a 422 rather than a 500 traceback.

    **Async build contract (200 vs 202).** When the off-orchestrator builder
    backend is enabled (``COMPUTE_IMAGE_BUILDER_BACKEND_ENABLED``) and a real
    build is needed, the build is dispatched to a throwaway builder VM and this
    returns **202 Accepted** with ``{status, build_id, builder_vm_id, dep_hash,
    async: true, adopted}`` plus the legacy fields as unresolved placeholders
    (``pinned_ref``/``digest`` null, ``built`` false). The build is finalized
    asynchronously by the reaper poll sweep; poll
    ``GET .../compute-image/status`` (which surfaces in-flight builds under
    ``active_builds``) to observe completion. An already-present image and a
    ``dry_run`` request still complete inline and return **200** with the
    persisted fields. When the backend flag is off the whole build runs inline
    and always returns 200 (the pre-existing behavior).

    **Requires OWNER role.** A build runs docker on the orchestrator host and
    pushes to the shared compute-image registry — a privileged, expensive,
    host-affecting operation, not the member-grade read/save that the rest of
    this router exposes. It is gated with the same role the other
    state-changing tool endpoints (register/seed/delete) already require.

    Concurrency-bounded: returns **429** (with ``Retry-After``) when a build is
    already in flight, and **504** when the pipeline exceeds its outer deadline —
    at which point the build is ABORTED (its docker children are killed) rather
    than left running headless; the concurrency slot is released when the build
    itself ends, not when this request does. A ``dry_run`` request performs no
    docker work but still takes a slot — it clones the repo and hashes the deps,
    which is the cheap part of the same pipeline.

    The Dockerfile's RUN steps are repo-controlled, so they run on a confined
    build network (``none`` by default); a 422 is returned if the configured
    network cannot be proven to block the GCP metadata server.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    manifest = await _discover_manifest_tools(project)
    build_config = _cb.resolve_project_compute_image_build(manifest.get("compute"))
    if build_config is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Project declares no compute.image.build section in "
                ".blueprint/tools.json — nothing to build."
            ),
        )

    registry_repo = _cb.resolve_compute_image_registry_repo()
    image_name = str(build_config["name"]).rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]

    # NON-BLOCKING slot acquisition: shed load rather than queue behind a build.
    if _compute_image_build_semaphore.locked():
        raise HTTPException(
            status_code=429,
            detail=(
                "a compute-image build is already running on the orchestrator "
                "(concurrency limit "
                f"{_MAX_CONCURRENT_IMAGE_BUILDS}) — retry shortly"
            ),
            headers={"Retry-After": str(_COMPUTE_IMAGE_BUILD_RETRY_AFTER_SECONDS)},
        )

    try:
        await asyncio.wait_for(
            _compute_image_build_semaphore.acquire(),
            timeout=1.0,
        )
    except asyncio.TimeoutError:
        # Lost the race between the `.locked()` check and acquire — same answer.
        raise HTTPException(
            status_code=429,
            detail=(
                "a compute-image build is already running on the orchestrator "
                "(concurrency limit "
                f"{_MAX_CONCURRENT_IMAGE_BUILDS}) — retry shortly"
            ),
            headers={"Retry-After": str(_COMPUTE_IMAGE_BUILD_RETRY_AFTER_SECONDS)},
        ) from None

    # The build runs as its own TASK, carrying a kill switch (BuildControl) that the
    # deadline path can use to actually stop the docker subprocesses. The slot is
    # handed to the task and released when the TASK ends — see
    # _release_image_build_slot for why releasing it in this request's `finally`
    # broke the bound on exactly the path (504) where it had to hold.
    control = _cib.BuildControl()
    token = _cib.set_build_control(control)
    try:
        build_task = asyncio.create_task(
            _cb.build_project_compute_image(
                project,
                registry_repo=registry_repo,
                build_config=build_config,
                dry_run=dry_run,
            )
        )
    except BaseException:
        _compute_image_build_semaphore.release()
        raise
    finally:
        _cib.reset_build_control(token)
    build_task.add_done_callback(_release_image_build_slot)

    try:
        # shield: wait_for must not just *abandon* the build on timeout — we want the
        # task object alive so we can abort it and watch it unwind.
        result = await asyncio.wait_for(
            asyncio.shield(build_task),
            timeout=_COMPUTE_IMAGE_BUILD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.exception(
            "[COMPUTE-IMAGE] build for project=%s exceeded %ss deadline — aborting it",
            project_id,
            _COMPUTE_IMAGE_BUILD_TIMEOUT_SECONDS,
        )
        await _abort_and_unwind_image_build(control, build_task, project_id)
        raise HTTPException(
            status_code=504,
            detail=(
                "compute-image build exceeded the "
                f"{_COMPUTE_IMAGE_BUILD_TIMEOUT_SECONDS}s deadline and was aborted"
            ),
        ) from None
    except asyncio.CancelledError:
        # Client disconnect / application shutdown: do not abandon the remote
        # build or release its concurrency slot until Cloud Build has observed
        # cancellation and the worker has unwound its staged-source cleanup.
        await asyncio.shield(
            _abort_and_unwind_image_build(control, build_task, project_id)
        )
        raise
    except _cib.BuildNetworkError as exc:
        # The build network could not be proven to block the metadata server.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ASYNC CONTRACT: when the off-orchestrator builder backend is enabled and a
    # real build is needed, the build was handed to a throwaway builder VM and is
    # NOT finished — the reaper poll sweep finalizes the ProjectComputeImage pin.
    # Return 202 with the tracking id instead of the synchronous build record.
    # An already-present image or a ``dry_run`` request still completes inline and
    # returns 200 with the persisted fields below, so existing callers that expect
    # a synchronous result keep working for those cases; a caller that triggers a
    # real async build MUST handle 202 (the ``async`` flag marks it).
    if isinstance(result, _cb.AsyncImageBuildDispatch):
        return Response(
            content=_json.dumps(
                {
                    "status": result.status,
                    "build_id": str(result.build_id),
                    "builder_vm_id": result.builder_vm_id,
                    "dep_hash": result.dep_hash,
                    "async": True,
                    "adopted": result.adopted,
                    # Backward-compat fields the admin-ui already reads — present
                    # but unresolved because the build has not completed yet.
                    "pinned_ref": None,
                    "digest": None,
                    "tag": result.dep_hash,
                    "built": False,
                    "already_present": False,
                }
            ),
            media_type="application/json",
            status_code=202,
        )

    result = await _preserve_existing_digest_pin(session, project.id, result)

    row = await _cb.record_project_compute_image_build(
        session,
        project.id,
        registry_repo,
        image_name,
        result,
    )
    await session.commit()
    await session.refresh(row)

    already_present = getattr(result, "already_present", not row.built)
    return {
        "pinned_ref": row.pinned_ref,
        "digest": row.digest,
        "dep_hash": row.dep_hash,
        "tag": row.tag,
        "built": row.built,
        "already_present": already_present,
    }


# ── Compute-image status endpoint ─────────────────────────────────────────────
#
# A read-only, owner-gated endpoint that surfaces the persisted build record
# (pinned_ref, digest, dep_hash, tag, built, timestamps) and reports whether the
# stored reference is genuinely digest-pinned vs. a mutable-tag fallback.  An
# optional ``?check_drift=true`` query param triggers a repo clone + hash cycle
# (identical to the build path's hash computation) to detect staleness.


async def _compute_image_build_metrics(
    session: AsyncSession,
    project_id: uuid.UUID,
    registry_repo: str,
    image_name: str,
    *,
    lookback_days: int = 30,
    limit: int = 100,
) -> Dict[str, Any]:
    """Aggregate operational metrics over terminal compute-image build rows.

    Queries up to ``limit`` terminal (SUCCEEDED/FAILED/TIMED_OUT)
    ``ProjectComputeImageBuild`` rows for this project/image within
    ``lookback_days``, newest first, and computes:

    - ``total_builds``, ``succeeded``, ``failed``, ``timed_out``
    - ``success_rate``: succeeded / total (null when no builds)
    - ``cache_hit_rate``: fraction of SUCCEEDED rows whose ``result_json``
      records ``already_present=true`` (null when no succeeded builds)
    - ``avg_build_seconds``: mean of ``image_published_at - started_at`` for
      SUCCEEDED rows that have both timestamps (null when none qualify)
    - ``in_flight_count``: PENDING + BUILDING rows (cross-checks active_builds)
    """
    cutoff = _utcnow() - timedelta(days=lookback_days)

    terminal_rows = (
        (
            await session.execute(
                select(ProjectComputeImageBuild)
                .where(
                    ProjectComputeImageBuild.project_id == project_id,
                    ProjectComputeImageBuild.registry_repo == registry_repo,
                    ProjectComputeImageBuild.image_name == image_name,
                    ProjectComputeImageBuild.status.in_(
                        [
                            ComputeImageBuildStatus.SUCCEEDED,
                            ComputeImageBuildStatus.FAILED,
                            ComputeImageBuildStatus.TIMED_OUT,
                        ]
                    ),
                    ProjectComputeImageBuild.created_at >= cutoff,
                )
                .order_by(ProjectComputeImageBuild.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    succeeded = [r for r in terminal_rows if r.status == ComputeImageBuildStatus.SUCCEEDED]
    failed = [r for r in terminal_rows if r.status == ComputeImageBuildStatus.FAILED]
    timed_out = [r for r in terminal_rows if r.status == ComputeImageBuildStatus.TIMED_OUT]
    total = len(terminal_rows)

    success_rate: float | None = (len(succeeded) / total) if total > 0 else None

    cache_hits = sum(
        1
        for r in succeeded
        if r.result_json and _json.loads(r.result_json).get("already_present")
    )
    cache_hit_rate: float | None = (cache_hits / len(succeeded)) if succeeded else None

    durations = [
        (r.image_published_at - r.started_at).total_seconds()
        for r in succeeded
        if r.started_at and r.image_published_at
    ]
    avg_build_seconds: float | None = (sum(durations) / len(durations)) if durations else None

    in_flight_count_result = await session.execute(
        select(func.count(ProjectComputeImageBuild.id)).where(
            ProjectComputeImageBuild.project_id == project_id,
            ProjectComputeImageBuild.registry_repo == registry_repo,
            ProjectComputeImageBuild.image_name == image_name,
            ProjectComputeImageBuild.status.in_(
                [ComputeImageBuildStatus.PENDING, ComputeImageBuildStatus.BUILDING]
            ),
        )
    )
    in_flight_count = in_flight_count_result.scalar() or 0

    return {
        "lookback_days": lookback_days,
        "total_builds": total,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "timed_out": len(timed_out),
        "success_rate": success_rate,
        "cache_hit_rate": cache_hit_rate,
        "avg_build_seconds": avg_build_seconds,
        "in_flight_count": in_flight_count,
    }


async def _list_active_image_builds(
    session: AsyncSession,
    project_id: uuid.UUID,
    registry_repo: str,
    image_name: str,
) -> list[Dict[str, Any]]:
    """Serialize in-flight (PENDING/BUILDING) off-orchestrator builds for an image.

    Returns the async-build tracking rows the reaper poll sweep has not yet
    finalized, newest first, so the status endpoint can surface a dispatched
    build before its ``ProjectComputeImage`` pin exists. Empty list when the
    async builder lane is unused (the common case today).
    """
    rows = (
        (
            await session.execute(
                select(ProjectComputeImageBuild)
                .where(
                    ProjectComputeImageBuild.project_id == project_id,
                    ProjectComputeImageBuild.registry_repo == registry_repo,
                    ProjectComputeImageBuild.image_name == image_name,
                    ProjectComputeImageBuild.status.in_(
                        [
                            ComputeImageBuildStatus.PENDING,
                            ComputeImageBuildStatus.BUILDING,
                        ]
                    ),
                )
                .order_by(ProjectComputeImageBuild.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "build_id": str(b.id),
            "status": b.status.value if hasattr(b.status, "value") else str(b.status),
            "dep_hash": b.dep_hash,
            "builder_vm_id": b.builder_vm_id,
            "started_at": b.started_at.isoformat() if b.started_at else None,
            "deadline_at": b.deadline_at.isoformat() if b.deadline_at else None,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in rows
    ]


@router.get("/projects/{project_id}/compute-image/status")
async def get_project_compute_image_status(
    project_id: uuid.UUID,
    check_drift: bool = Query(
        False,
        description=(
            "When true, clone the repo and compute the current dependency hash "
            "(identical to the build path) and report whether it differs from "
            "the stored build record's dep_hash.  This is an expensive operation "
            "(git clone + file discovery + hashing) and competes with the build "
            "concurrency slot — a build in flight causes drift to return as "
            "'unknown' with a reason rather than waiting."
        ),
    ),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Get the persisted baked-image build record for this project (owner).

    Returns the ``ProjectComputeImage`` row state for the project's declared
    compute image.  ``is_digest_pinned`` distinguishes a genuine digest-pinned
    ref from the mutable-tag fallback path (previously a log-only signal).  When
    no build has ever been triggered the endpoint reports ``never_built``, and
    when the project does not declare a compute image it reports
    ``no_compute_image_declared`` — both as clean status values, not errors.

    ``active_builds`` (present on ``built`` and ``never_built`` responses) lists
    any in-flight off-orchestrator builds (PENDING/BUILDING
    ``ProjectComputeImageBuild`` rows) the reaper poll sweep has not yet
    finalized — this is how a caller polls an async build dispatched by the
    trigger endpoint's 202 path. It is an empty list when no async build is in
    flight (the common case today).

    The optional ``?check_drift=true`` param adds a ``drift`` object to the
    response, computed by cloning the project repo and running the same
    dependency-hash computation as the build pipeline (``dry_run=True``).  The
    clone uses the build concurrency slot and is bounded by a 5-minute timeout.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    manifest = await _discover_manifest_tools(project)
    compute_section = manifest.get("compute") or {}
    compute_image = compute_section.get("image")

    if compute_image is None:
        return {"status": "no_compute_image_declared"}

    name = compute_image.get("name", "")
    registry_repo, image_name, _ = _cb._split_image_ref(name)

    # In-flight off-orchestrator builds (PENDING/BUILDING) for this exact image,
    # surfaced alongside the durable ProjectComputeImage record so an operator can
    # see an async build that has been dispatched but not yet finalized. Additive:
    # the existing ``status`` values are unchanged.
    active_builds = await _list_active_image_builds(
        session, project_id, registry_repo, image_name
    )

    # Query the latest ProjectComputeImage row matching this project/image.
    row = (
        await session.execute(
            select(ProjectComputeImage)
            .where(
                ProjectComputeImage.project_id == project_id,
                ProjectComputeImage.image_name == image_name,
                ProjectComputeImage.registry_repo == registry_repo,
            )
            .order_by(ProjectComputeImage.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        return {"status": "never_built", "active_builds": active_builds}

    build_metrics = await _compute_image_build_metrics(
        session, project_id, registry_repo, image_name
    )

    build_age_hours: float | None = None
    if row.updated_at:
        build_age_hours = (_utcnow() - row.updated_at).total_seconds() / 3600.0

    result: Dict[str, Any] = {
        "status": "built",
        "pinned_ref": row.pinned_ref,
        "digest": row.digest,
        "dep_hash": row.dep_hash,
        "tag": row.tag,
        "built": bool(row.built),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "is_digest_pinned": row.digest is not None,
        "active_builds": active_builds,
        "build_metrics": build_metrics,
        "build_age_hours": build_age_hours,
    }

    if check_drift:
        drift = await _compute_build_drift(
            project, registry_repo, compute_image, row
        )
        result["drift"] = drift

    return result


async def _compute_build_drift(
    project: Project,
    registry_repo: str,
    compute_image: Dict[str, Any],
    stored_row: ProjectComputeImage,
) -> Dict[str, Any]:
    """Compute whether the project's dependency hash has drifted from the build record.

    Returns a dict with ``drifted`` (True/False/None) and, when drifted is not
    None, both ``current_dep_hash`` and ``stored_dep_hash``.  When drift cannot
    be determined, ``drifted`` is None and ``reason`` explains why.

    This clones the project repo and runs the same file-discovery +
    content-hashing logic as the build pipeline (identical input set), so the
    comparison is apples-to-apples.  It competes for the build concurrency slot
    to bound concurrent clones, and is fail-open: a build in flight, a missing
    GitHub token, or a missing ``build`` section all produce ``drifted: None``
    with a ``reason`` rather than a 500.
    """
    build_config = _cb.resolve_project_compute_image_build(
        {"image": compute_image}
    )
    if build_config is None:
        return {
            "drifted": None,
            "reason": (
                "Project declares no compute.image.build section "
                "— cannot recompute dependency hash"
            ),
        }

    # Fail-open: if a build is in flight, don't queue behind it — return
    # unknown drift rather than potentially blocking an operator-facing request.
    if _compute_image_build_semaphore.locked():
        return {
            "drifted": None,
            "reason": (
                "A compute-image build is in progress — drift check would "
                "contend with it"
            ),
        }

    try:
        await asyncio.wait_for(
            _compute_image_build_semaphore.acquire(),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        return {
            "drifted": None,
            "reason": "Could not acquire build slot for drift computation",
        }

    try:
        result = await asyncio.wait_for(
            _cb.build_project_compute_image(
                project,
                registry_repo=registry_repo,
                build_config=build_config,
                dry_run=True,
            ),
            timeout=300,
        )
    except asyncio.TimeoutError:
        return {
            "drifted": None,
            "reason": "Drift computation timed out while computing dependency hash",
        }
    except Exception as e:
        logger.warning(
            "[compute-image-status] drift computation failed for project=%s: %s",
            getattr(project, "id", "?"),
            e,
        )
        return {
            "drifted": None,
            "reason": f"Could not compute current dependency hash: {e}",
        }
    finally:
        _compute_image_build_semaphore.release()

    current_hash = getattr(result, "dep_hash", None)
    if not current_hash:
        return {
            "drifted": None,
            "reason": "Build pipeline returned no dep_hash",
        }

    return {
        "drifted": current_hash != stored_row.dep_hash,
        "current_dep_hash": current_hash,
        "stored_dep_hash": stored_row.dep_hash,
    }


@router.get("/compute-image-registry/readiness")
async def get_compute_image_registry_readiness(
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Return the GC-guard sweep's readiness signal for the compute-image registry.

    The compute-image Artifact Registry has ``cleanup_policy_dry_run=true``,
    meaning image storage grows unbounded until the operator can confirm the
    ``inuse-`` protection-tag guard sweep is working reliably.  This endpoint
    surfaces the machine-readable evidence accumulated since process start so
    the operator can make the ``dry_run=false`` flip with confidence, without
    resorting to log inspection.

    Response fields mirror ``reaper_sweeps.get_compute_image_gc_guard_readiness``:
      - **ready** (bool) — ``True`` when at least one sweep run completed with
        protected digests and zero apply failures on the last execution.
      - **total_runs** (int) — number of actual sweep executions since start.
      - **consecutive_healthy_cycles** (int) — consecutive runs with zero failures.
      - **first_run_at** / **last_run_at** — ISO-format timestamps (null before
        first run).
      - **last_sweep** — per-run snapshot ``{applied, pruned, apply_failed}``
        from the most recent execution (null before first run).
      - **cumulative** — running totals ``{applied, pruned, apply_failed}``.
    """
    return _rs.get_compute_image_gc_guard_readiness()
