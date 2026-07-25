"""Resolve the actionable compute-image environment for an agent.

This is deliberately a *resolver*, not a policy gate.  Projects without a
declared image continue to run bare compute; the response merely tells an
agent whether an image is ready, what project change would make one buildable,
or that there is no credible Dockerfile candidate to act on.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import Project


class ComputeImageEnvironmentState(StrEnum):
    """Stable states returned by the agent-facing environment resolver."""

    READY = "ready"
    MISSING_DOCKERFILE_CANDIDATE = "missing_but_dockerfile_candidate"
    DECLARED_NEEDS_BUILD = "declared_needs_build"
    NO_CANDIDATE = "no_candidate"


_DOCKERFILE_NAMES = frozenset({"dockerfile", "dockerfile.compute"})
_CANDIDATE_EXCLUDED_PARTS = frozenset({"node_modules", ".venv", "vendor"})


def _candidate_sort_key(path: str) -> tuple[int, int, str]:
    """Prefer purpose-built images, then shallow conventional Dockerfiles."""
    normalized = path.replace("\\", "/")
    if normalized == ".blueprint/Dockerfile.compute":
        return (0, 0, normalized)
    if normalized == "Dockerfile":
        return (1, 0, normalized)
    return (2, normalized.count("/"), normalized)


def choose_dockerfile_candidate(paths: list[str] | None) -> str | None:
    """Choose one deterministic, repository-tracked Dockerfile candidate."""
    candidates: list[str] = []
    for raw_path in paths or []:
        if not isinstance(raw_path, str):
            continue
        path = raw_path.strip().replace("\\", "/")
        if not path or path.rsplit("/", 1)[-1].lower() not in _DOCKERFILE_NAMES:
            continue
        if any(part.lower() in _CANDIDATE_EXCLUDED_PARTS for part in path.split("/")):
            continue
        candidates.append(path)
    return min(candidates, key=_candidate_sort_key) if candidates else None


def _suggested_image_name(project: "Project") -> str:
    from app.compute_backends import resolve_compute_image_registry_repo

    raw_name = "-".join(
        filter(None, (getattr(project, "github_owner", None), getattr(project, "github_repo", None)))
    ).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw_name).strip("-")[:63]
    if not slug:
        slug = f"project-{str(getattr(project, 'id', 'image'))[:8]}"
    return f"{resolve_compute_image_registry_repo()}/{slug}:latest"


async def _list_project_paths(project: "Project") -> list[str] | None:
    """Return tracked paths at the default branch, or ``None`` on safe failure."""
    owner = (getattr(project, "github_owner", None) or "").strip()
    repo = (getattr(project, "github_repo", None) or "").strip()
    if not owner or not repo:
        return None
    from app.github import list_repo_tree_paths_at_ref
    from app.github_auth import get_repo_installation_read_token
    from app.github_refs import get_repo_default_branch

    token = await get_repo_installation_read_token(owner, repo)
    if not token:
        return None
    branch = await get_repo_default_branch(token, owner, repo)
    tree = await list_repo_tree_paths_at_ref(
        token,
        owner,
        repo,
        branch,
        project_id=str(getattr(project, "id", "")),
    )
    if tree is None:
        return None
    return [
        entry["path"]
        for entry in tree
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]


async def resolve_project_compute_image_environment(
    session: "AsyncSession",
    project: "Project",
    *,
    manifest_fn: Callable[["Project"], Awaitable[dict[str, Any]]] | None = None,
    path_list_fn: Callable[["Project"], Awaitable[list[str] | None]] | None = None,
) -> dict[str, Any]:
    """Return an actionable, non-blocking image environment for *project*.

    ``ready`` supplies the digest-pinned image that the normal compute launch
    path already defaults to.  The other states are guidance only: neither a
    missing manifest nor a failed candidate scan changes bare-compute behavior.
    """
    if manifest_fn is None:
        from app.routers.admin_project_tools import _discover_manifest_tools

        async def _fresh_manifest(project_arg: "Project") -> dict[str, Any]:
            return await _discover_manifest_tools(project_arg, force_refresh=True)

        manifest_fn = _fresh_manifest
    if path_list_fn is None:
        path_list_fn = _list_project_paths

    manifest = await manifest_fn(project)
    compute_image = ((manifest.get("compute") or {}).get("image") or {})
    if isinstance(compute_image, dict) and compute_image.get("name"):
        from app.compute_backends import resolve_project_baked_image_strict

        pinned_ref, resolution_error = await resolve_project_baked_image_strict(
            session, project
        )
        if pinned_ref:
            return {
                "state": ComputeImageEnvironmentState.READY,
                "action": "launch",
                "container_image": pinned_ref,
                "manifest_image": compute_image["name"],
                "launch_guidance": (
                    "The platform defaults compute_experiment launches to this "
                    "digest-pinned image. Omit container_image unless a different "
                    "runtime is intentionally required."
                ),
            }

        build = compute_image.get("build")
        if isinstance(build, dict):
            return {
                "state": ComputeImageEnvironmentState.DECLARED_NEEDS_BUILD,
                "action": "build",
                "manifest_image": compute_image["name"],
                "dockerfile": build.get("dockerfile") or ".blueprint/Dockerfile.compute",
                "next_step": "Run ./compute-image build, then re-check status before launching.",
                **({"reason": resolution_error} if resolution_error else {}),
            }
        return {
            "state": ComputeImageEnvironmentState.DECLARED_NEEDS_BUILD,
            "action": "add_build_config",
            "manifest_image": compute_image["name"],
            "next_step": (
                "The image is declared but has no build recipe. Add compute.image.build "
                "to .blueprint/tools.json before requesting a build."
            ),
            **({"reason": resolution_error} if resolution_error else {}),
        }

    candidate = choose_dockerfile_candidate(await path_list_fn(project))
    if candidate:
        image_name = _suggested_image_name(project)
        return {
            "state": ComputeImageEnvironmentState.MISSING_DOCKERFILE_CANDIDATE,
            "action": "add_manifest_build",
            "candidate_dockerfile": candidate,
            "manifest_template": {
                "compute": {
                    "image": {
                        "name": image_name,
                        "build": {"dockerfile": candidate},
                    }
                }
            },
            "next_step": (
                "Create a project change adding this compute.image declaration to "
                ".blueprint/tools.json; after it lands, run ./compute-image build."
            ),
        }

    return {
        "state": ComputeImageEnvironmentState.NO_CANDIDATE,
        "action": "use_bare_compute",
        "container_image": None,
        "next_step": (
            "No repository Dockerfile candidate was found. Bare compute remains valid; "
            "only add an image when the workload needs a container-only runtime."
        ),
    }
