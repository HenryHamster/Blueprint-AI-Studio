"""Project onboarding endpoint with build config auto-detection."""
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter
from app.models import Project, ProjectMember, ProjectStatus, _utcnow
from app.auth import get_current_user, require_admin
from app.schemas import OnboardPayload

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-onboard"],
    dependencies=[Depends(get_current_user)]
)


async def _detect_build_config(token: str, owner: str, repo: str) -> Dict[str, Any]:
    """Auto-detect build configuration by inspecting the repo's root files via GitHub API."""
    from app.github import _github_request

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Fetch root directory listing
    resp = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/contents/", headers,
        max_retries=2,
    )
    if resp.status_code != 200:
        logger.warning(f"[ONBOARD] Failed to list repo contents for {owner}/{repo}: {resp.status_code}")
        return {}

    root_files = {item["name"]: item for item in resp.json() if isinstance(item, dict)}
    root_names = set(root_files.keys())

    directories: list[Dict[str, Any]] = []
    languages: list[str] = []

    # Detect Python project
    if "pyproject.toml" in root_names or "setup.py" in root_names or "setup.cfg" in root_names:
        languages.append("python")
        directories.append({
            "path": "./",
            "type": "python",
            "lint_cmd": "ruff check ." if "pyproject.toml" in root_names else None,
            "test_cmd": "pytest",
        })

    # Detect Node/TypeScript project (root-level)
    if "package.json" in root_names:
        lang = "typescript" if "tsconfig.json" in root_names else "typescript"
        languages.append(lang)
        directories.append({
            "path": "./",
            "type": "node",
            "install_cmd": "npm ci",
            "lint_cmd": "npx eslint .",
            "build_cmd": "npm run build",
            "test_cmd": "npm test",
        })

    # Detect Go project
    if "go.mod" in root_names:
        languages.append("go")
        directories.append({
            "path": "./",
            "type": "go",
            "lint_cmd": "go vet ./...",
            "test_cmd": "go test ./...",
        })

    # Detect Rust project
    if "Cargo.toml" in root_names:
        languages.append("rust")
        directories.append({
            "path": "./",
            "type": "rust",
            "lint_cmd": "cargo clippy",
            "build_cmd": "cargo build",
            "test_cmd": "cargo test",
        })

    # Scan subdirectories for multi-project repos
    subdirs = [item["name"] for item in resp.json()
               if isinstance(item, dict) and item.get("type") == "dir"]

    for subdir in subdirs:
        sub_resp = await _github_request(
            "GET", f"https://api.github.com/repos/{owner}/{repo}/contents/{subdir}",
            headers, max_retries=1,
        )
        if sub_resp.status_code != 200:
            continue
        sub_files = {item["name"] for item in sub_resp.json() if isinstance(item, dict)}

        if "package.json" in sub_files and not any(d["path"] == f"{subdir}/" for d in directories):
            directories.append({
                "path": f"{subdir}/",
                "type": "node",
                "install_cmd": "npm ci",
                "lint_cmd": "npx eslint .",
                "build_cmd": "npm run build",
            })
            if "typescript" not in languages:
                languages.append("typescript")

        if ("pyproject.toml" in sub_files or "setup.py" in sub_files) and not any(
            d["path"] == f"{subdir}/" for d in directories
        ):
            directories.append({
                "path": f"{subdir}/",
                "type": "python",
                "lint_cmd": "ruff check .",
                "test_cmd": "pytest",
            })
            if "python" not in languages:
                languages.append("python")

    # Determine primary language
    if len(languages) == 0:
        language = None
    elif len(languages) == 1:
        language = languages[0]
    else:
        language = "mixed"

    # Try to read CLAUDE.md for additional instructions
    claude_md_resp = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/contents/CLAUDE.md",
        headers, max_retries=1,
    )
    # We don't parse CLAUDE.md into build_config, but its presence is noted

    config: Dict[str, Any] = {
        "language": language,
        "directories": directories,
        "env_vars": {},
        "git_user_name": "Blueprint Agent",
        "git_user_email": "blueprint-agent@automated.dev",
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }

    return config


@router.post("/projects/onboard")
@limiter.limit("10/minute")
async def onboard_project(
    request: Request,
    payload: OnboardPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Create a new project with auto-detected build configuration from the GitHub repo."""

    # Create project record
    project_name = payload.name or f"{payload.github_owner}/{payload.github_repo}"
    project = Project(
        name=project_name,
        user_id=current_user["user_id"],
        status=ProjectStatus.DEPLOYING,
        github_owner=payload.github_owner,
        github_repo=payload.github_repo,
    )
    session.add(project)
    await session.flush()  # Get project.id before membership insert

    # Create owner membership
    member = ProjectMember(
        project_id=project.id,
        user_id=current_user["user_id"],
        role="owner",
    )
    session.add(member)

    # Auto-detect build config from GitHub repo
    build_config: Dict[str, Any] = {}
    try:
        from app.github import get_repo_installation_token
        token = await get_repo_installation_token(payload.github_owner, payload.github_repo)
        if token:
            build_config = await _detect_build_config(token, payload.github_owner, payload.github_repo)
        else:
            logger.warning(f"[ONBOARD] No GitHub token available for {payload.github_owner}/{payload.github_repo}")
    except Exception as e:
        logger.error(f"[ONBOARD] Build config detection failed for {payload.github_owner}/{payload.github_repo}: {e}", exc_info=True)

    # Merge override on top of detected config
    if payload.build_config_override:
        build_config.update(payload.build_config_override)

    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)

    await session.commit()
    await session.refresh(project)

    return {
        "project_id": str(project.id),
        "build_config": project.build_config or {},
    }
