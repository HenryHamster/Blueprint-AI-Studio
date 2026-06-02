"""Project onboarding endpoint with build config auto-detection.

The onboarding flow creates the project row, assigns owner membership, tries
to infer build commands from the GitHub repository root, seeds a stable
task-branch namespace, and then validates the merged configuration against the
BuildConfig schema before saving it.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter
from app.models import Project, ProjectMember, ProjectStatus, ProjectMessage, MessageType
from app.auth import get_current_user, require_admin
from app.schemas import OnboardPayload
from app.task_locking import with_design_doc_locked

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-onboard"],
    dependencies=[Depends(get_current_user)],
)


def _derive_task_branch_namespace(github_repo: str) -> str:
    """Derive a branch-name-safe namespace from the GitHub repo name.

    Lowercases, replaces non-alphanumeric characters with '-', strips
    leading/trailing dashes, and caps at 40 characters.  The Blueprint
    self-repo maps to 'blueprint' via the is_self_repo flag at the DB layer;
    this function slugifies all repo names uniformly.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", github_repo.lower()).strip("-")
    return slug[:40] if slug else "project"


async def _detect_build_config(token: str, owner: str, repo: str) -> Dict[str, Any]:
    """Auto-detect build configuration by inspecting the repo's root files.

    The detector is intentionally shallow: it looks at the repository root and
    one level of subdirectories to seed directory-specific install/lint/build
    commands, then returns a BuildConfig-shaped dict that onboarding can merge
    with operator overrides.
    """
    from app.github import _github_request

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Fetch root directory listing
    resp = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/contents/",
        headers,
        max_retries=2,
    )
    if resp.status_code != 200:
        logger.warning(
            f"[ONBOARD] Failed to list repo contents for {owner}/{repo}: {resp.status_code}"
        )
        return {}

    root_files = {item["name"]: item for item in resp.json() if isinstance(item, dict)}
    root_names = set(root_files.keys())

    directories: list[Dict[str, Any]] = []
    languages: list[str] = []

    # Detect Python project
    if (
        "pyproject.toml" in root_names
        or "setup.py" in root_names
        or "setup.cfg" in root_names
    ):
        languages.append("python")
        directories.append(
            {
                "path": "./",
                "type": "python",
                "lint_cmd": "ruff check",
                "test_cmd": "python3 -m pytest",
            }
        )

    # Detect Node/TypeScript project (root-level)
    if "package.json" in root_names:
        languages.append("typescript")
        directories.append(
            {
                "path": "./",
                "type": "node",
                "install_cmd": "npm ci",
                "build_cmd": "npm run build",
                "test_cmd": "npm test",
            }
        )

    # Detect Go project
    if "go.mod" in root_names:
        languages.append("go")
        directories.append(
            {
                "path": "./",
                "type": "go",
                "test_cmd": "go test ./...",
            }
        )

    # Detect Rust project
    if "Cargo.toml" in root_names:
        languages.append("rust")
        directories.append(
            {
                "path": "./",
                "type": "rust",
                "build_cmd": "cargo build",
                "test_cmd": "cargo test",
            }
        )

    # Scan subdirectories for multi-project repos
    subdirs = [
        item["name"]
        for item in resp.json()
        if isinstance(item, dict) and item.get("type") == "dir"
    ]

    for subdir in subdirs:
        sub_resp = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/contents/{subdir}",
            headers,
            max_retries=1,
        )
        if sub_resp.status_code != 200:
            continue
        sub_files = {item["name"] for item in sub_resp.json() if isinstance(item, dict)}

        if "package.json" in sub_files and not any(
            d["path"] == f"{subdir}/" for d in directories
        ):
            directories.append(
                {
                    "path": f"{subdir}/",
                    "type": "node",
                    "install_cmd": "npm ci",
                    "lint_cmd": "npx eslint .",
                    "build_cmd": "npm run build",
                }
            )
            if "typescript" not in languages:
                languages.append("typescript")

        if ("pyproject.toml" in sub_files or "setup.py" in sub_files) and not any(
            d["path"] == f"{subdir}/" for d in directories
        ):
            directories.append(
                {
                    "path": f"{subdir}/",
                    "type": "python",
                    "lint_cmd": "ruff check",
                    "test_cmd": "python3 -m pytest",
                }
            )
            if "python" not in languages:
                languages.append("python")

    # Determine primary language
    if len(languages) == 0:
        language = None
    elif len(languages) == 1:
        language = languages[0]
    else:
        language = "mixed"

    config: Dict[str, Any] = {
        "language": language,
        "directories": directories,
        "env_vars": {},
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }

    return config


def _seed_strategic_intent(
    project: Project, goal: str | None, steering: Dict[str, Any] | None
) -> ProjectMessage | None:
    """Seed the user's onboarding goal into the project's strategic intent.

    Blueprint's bootstrap reads the user's intent from two channels on the first
    strategic cycle: ``problem_model.intent`` and the user-message conversation
    thread. This writes the goal into BOTH so cycle 1 reasons from the user's
    actual ask rather than a blank slate:

    1. ``problem_model.intent.purpose`` + ``current_focus`` (with a
       ``user_message`` provenance source), plus any optional steering merged
       into the same user-editable ``context`` / ``constraints`` subset the
       post-creation PATCH path accepts.
    2. A returned ``from_type='user'`` ProjectMessage carrying the goal verbatim
       so it lands in ``conversation_thread``.

    Returns ``None`` and makes no changes when the goal is empty, preserving
    today's behavior for users who skip it.
    """
    goal = (goal or "").strip()
    if not goal:
        return None

    from pydantic import ValidationError
    from app.problem_model import (
        apply_user_intent_patch,
        parse_problem_model,
        Source,
    )

    message = ProjectMessage(
        project_id=project.id,
        from_type="user",
        message_type=MessageType.NOTE,
        content=goal,
    )

    pm = parse_problem_model(project.problem_model)
    pm.intent.purpose = goal
    pm.intent.current_focus = goal
    pm.intent.source = Source(
        type="user_message", message_id=str(message.id), excerpt=goal[:120]
    )
    if steering:
        # Reuse only the user-editable subset (context / constraints); ignore
        # any agent-owned keys a caller might pass. Route the merge through the
        # shared apply_user_intent_patch helper so steering is validated the
        # same way the post-creation project-config edits are — a malformed
        # value (e.g. a bare string where ProjectContext is expected) surfaces
        # as a clean 400 rather than an unhandled ValidationError 500.
        patch = {k: steering[k] for k in ("context", "constraints") if k in steering}
        if patch:
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                pm = apply_user_intent_patch(pm.model_dump(), patch, now_iso)
            except (ValueError, ValidationError) as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid initial_steering: {e}",
                ) from e
    pm.last_updated_by = "user"
    project.problem_model = pm.model_dump()
    flag_modified(project, "problem_model")

    return message


@router.post("/projects/onboard")
@limiter.limit("10/minute")
async def onboard_project(
    request: Request,
    payload: OnboardPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Create a new project, seed membership, and persist detected build config.

    Flow:
    1. Create the Project row in DEPLOYING state.
    2. Add the caller as owner via ProjectMember.
    3. Attempt GitHub-based build-config detection.
    4. Seed ``task_branch_namespace`` from the repo name when missing.
    5. Apply any caller-provided build_config_override on top of detection.
    6. Validate against ``BuildConfig`` and store the validated shape when
       possible, or the raw detected dict if validation fails.
    """

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
    token = None
    try:
        from app.github import get_repo_installation_token

        token = await get_repo_installation_token(
            payload.github_owner, payload.github_repo
        )
        if token:
            build_config = await _detect_build_config(
                token, payload.github_owner, payload.github_repo
            )
        else:
            logger.warning(
                f"[ONBOARD] No GitHub token available for {payload.github_owner}/{payload.github_repo}"
            )
    except Exception as e:
        logger.error(
            f"[ONBOARD] Build config detection failed for {payload.github_owner}/{payload.github_repo}: {e}",
            exc_info=True,
        )

    # Seed per-project task-branch namespace. Operator overrides can still
    # replace it, but later project PATCH/re-detect flows intentionally preserve
    # the stored value once it exists.
    build_config.setdefault(
        "task_branch_namespace",
        _derive_task_branch_namespace(payload.github_repo),
    )

    # Merge caller-supplied overrides on top of detected config so explicit
    # onboarding input wins over repo heuristics.
    if payload.build_config_override:
        build_config.update(payload.build_config_override)

    # Validate final build_config structure
    from app.schemas import BuildConfig as BuildConfigSchema

    try:
        validated = (
            BuildConfigSchema(**build_config) if build_config else BuildConfigSchema()
        )
        project.build_config = validated.model_dump(exclude_none=True)
    except Exception as exc:  # noqa: BLE001
        # Detection + override produced invalid shape — store raw but log warning
        logger.warning("[ONBOARD] build_config failed schema validation, storing raw: %s", exc)
        project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)

    # Detect default branch and initialize auto_remediation config
    if not token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No installation token available — cannot detect default branch.",
        )
    from app.github import get_repo_default_branch

    try:
        default_branch = await get_repo_default_branch(
            token, payload.github_owner, payload.github_repo
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[ONBOARD] Default branch detection failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Default branch detection failed: {e}",
        ) from e

    if default_branch:
        project.integration_branch = default_branch
        project.promotion_target = default_branch

    if payload.promotion_target:
        project.promotion_target = payload.promotion_target

    branch_whitelist: list[str] = []
    if default_branch:
        branch_whitelist.append(default_branch)
    if (
        project.integration_branch
        and project.integration_branch not in branch_whitelist
    ):
        branch_whitelist.append(project.integration_branch)
    if project.promotion_target and project.promotion_target not in branch_whitelist:
        branch_whitelist.append(project.promotion_target)
    # Route the one-time onboarding design_doc seed through the canonical
    # lock helper so this file satisfies the design_doc lock invariant without
    # a special-case exemption. There are no concurrent writers at onboard time
    # (the project was just created in this handler), but the helper's
    # SELECT FOR UPDATE re-load (populate_existing=True) would clobber the
    # build_config / integration_branch / promotion_target changes set above if
    # they were still dirty in the session. Flush them explicitly first so the
    # re-SELECT reloads the persisted values rather than overwriting the dirty
    # attributes with a stale row — this is independent of the session's
    # autoflush setting (production AsyncSessionLocal is autoflush=True; the
    # SQLite test session is autoflush=False).
    def _seed_remediation_config(doc: dict) -> None:
        doc["_auto_remediation"] = {
            "enabled": False,
            "branch_whitelist": branch_whitelist,
            "max_per_hour": 2,
            "max_concurrent": 2,
            "require_review": True,
        }
        doc["_issue_implementation"] = {
            "enabled": False,
            "trigger_label": "auto:implement",
            "max_per_hour": 3,
            "max_concurrent": 3,
            "default_effort": "high",
        }
        doc["_auto_review"] = {
            "enabled": False,
            "max_per_hour": 5,
            "max_concurrent": 3,
        }

    await session.flush()
    await with_design_doc_locked(session, project.id, _seed_remediation_config)

    # Seed the user's goal + optional steering into strategic intent so the
    # first strategic cycle reasons from it. Read defensively: the OnboardPayload
    # schema field lands via a sibling task, so until then this is a no-op.
    seeded_message = _seed_strategic_intent(
        project,
        getattr(payload, "goal", None),
        getattr(payload, "initial_steering", None),
    )
    if seeded_message is not None:
        session.add(seeded_message)
        session.add(project)

    await session.commit()
    await session.refresh(project)

    return {
        "project_id": str(project.id),
        "build_config": project.build_config or {},
        "default_branch": default_branch,
    }
