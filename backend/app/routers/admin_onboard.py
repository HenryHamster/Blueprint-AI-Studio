"""Project onboarding endpoint with build config auto-detection.

The onboarding flow creates the project row, assigns owner membership, tries
to infer build commands from the GitHub repository root, seeds a stable
task-branch namespace, and then validates the merged configuration against the
BuildConfig schema before saving it.
"""

import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any
from urllib.parse import quote

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

# Backend onboarding must stay aligned with the agent prompt-time greenfield
# detector: same threshold and same "real source" exclusions live there in
# agent/src/constants.ts and agent/src/prompts/discovery.ts.
GREENFIELD_SOURCE_FILE_THRESHOLD = 8
_NON_SOURCE_FILE_EXTENSIONS = (
    ".md",
    ".markdown",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".lock",
    ".lockb",
)
_NON_SOURCE_FILE_BASENAMES = frozenset(
    {
        "license",
        "licence",
        "notice",
        "authors",
        "codeowners",
        "dockerfile",
        "makefile",
        "procfile",
        "go.mod",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".gitmodules",
        ".dockerignore",
        ".npmrc",
        ".nvmrc",
        ".prettierrc",
        ".eslintrc",
        ".pre-commit-config.yaml",
    }
)
_NON_SOURCE_FILE_PREFIXES = (
    "readme",
    "contributing",
    "changelog",
    "security",
)
_BUILD_CONFIG_SCAN_MAX_DEPTH = 2
_BUILD_CONFIG_SCAN_MAX_DIRECTORIES = 40


def _is_real_source_file(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].strip().lower()
    if not base or base.startswith("."):
        return False
    if base in _NON_SOURCE_FILE_BASENAMES:
        return False
    if any(base.startswith(prefix) for prefix in _NON_SOURCE_FILE_PREFIXES):
        return False
    if any(base.endswith(ext) for ext in _NON_SOURCE_FILE_EXTENSIONS):
        return False
    return True


def _classify_repo_emptiness(
    root_items: list[Dict[str, Any]], listing_complete: bool = True
) -> bool:
    """Classify a repo as empty / near-empty from fetched contents listings.

    Returns True ONLY when the repository was read successfully and completely
    AND has at most ``GREENFIELD_SOURCE_FILE_THRESHOLD`` real source files across
    the fetched root listing and every nested directory listing the detector
    walked. Documentation/config/data files, dotfiles, and common manifests are
    not counted as source.

    ``listing_complete`` is the positive confirmation that every listing needed
    to make this determination was fetched. When it is False (e.g. a nested
    directory listing failed to fetch or the bounded scan hit unread deeper
    paths), emptiness is indeterminate and this returns False
    (treat-as-non-empty) — a repo whose real source lives in a subdirectory we
    could not read must never be bootstrapped as greenfield on a transient
    fetch error.

    ``root_items`` is the raw GitHub ``contents/`` listings already fetched by
    :func:`_detect_build_config`, so it adds zero GitHub cost.
    """
    if not listing_complete:
        return False

    source_file_count = 0
    for item in root_items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        item_type = item.get("type")
        if item_type != "file":
            continue
        if _is_real_source_file(name):
            source_file_count += 1
            if source_file_count > GREENFIELD_SOURCE_FILE_THRESHOLD:
                return False

    return True


async def _detect_build_config(
    token: str, owner: str, repo: str, ref: str | None = None
) -> Dict[str, Any]:
    """Auto-detect build configuration by inspecting a bounded repo subtree.

    The detector inspects the repository root plus nested subdirectories up to
    ``_BUILD_CONFIG_SCAN_MAX_DEPTH`` so common layouts like ``apps/web`` and
    ``services/api`` can emit usable directory commands during onboarding and
    owner-triggered re-detect.

    When ``ref`` is provided, every GitHub contents API call targets that
    branch/ref instead of the repository's default branch. When ``ref`` is
    None the requests are identical to the default-branch behavior.
    """
    from app.github import _github_request

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Only attach ?ref=... when a ref was requested, so default-branch
    # detection issues byte-identical requests to before.
    params = {"ref": ref} if ref is not None else None

    def _contents_url(path: str | None = None) -> str:
        if not path:
            return f"https://api.github.com/repos/{owner}/{repo}/contents/"
        return (
            f"https://api.github.com/repos/{owner}/{repo}/contents/"
            f"{quote(path, safe='/')}"
        )

    def _record_language(languages: list[str], language: str) -> None:
        if language not in languages:
            languages.append(language)

    def _normalized_directory_path(path: str) -> str:
        return "./" if not path else f"{path}/"

    def _add_detected_directory(
        directories: list[Dict[str, Any]],
        languages: list[str],
        path: str,
        project_type: str,
    ) -> None:
        normalized_path = _normalized_directory_path(path)
        if any(
            d["path"] == normalized_path and d.get("type") == project_type
            for d in directories
        ):
            return

        if project_type == "python":
            directories.append(
                {
                    "path": normalized_path,
                    "type": "python",
                    "lint_cmd": "ruff check",
                    "test_cmd": "python3 -m pytest",
                }
            )
            _record_language(languages, "python")
            return

        if project_type == "node":
            entry: Dict[str, Any] = {
                "path": normalized_path,
                "type": "node",
                "install_cmd": "npm ci",
                "build_cmd": "npm run build",
                "test_cmd": "npm test",
            }
            if path:
                entry["lint_cmd"] = "npx eslint ."
            directories.append(entry)
            _record_language(languages, "typescript")
            return

        if project_type == "go":
            directories.append(
                {
                    "path": normalized_path,
                    "type": "go",
                    "test_cmd": "go test ./...",
                }
            )
            _record_language(languages, "go")
            return

        if project_type == "rust":
            directories.append(
                {
                    "path": normalized_path,
                    "type": "rust",
                    "build_cmd": "cargo build",
                    "test_cmd": "cargo test",
                }
            )
            _record_language(languages, "rust")

    def _detect_projects_in_listing(
        path: str,
        listing: list[Dict[str, Any]],
        directories: list[Dict[str, Any]],
        languages: list[str],
    ) -> None:
        names = {
            item.get("name")
            for item in listing
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }

        if {"pyproject.toml", "setup.py", "setup.cfg"} & names:
            _add_detected_directory(directories, languages, path, "python")
        if "package.json" in names:
            _add_detected_directory(directories, languages, path, "node")
        if "go.mod" in names:
            _add_detected_directory(directories, languages, path, "go")
        if "Cargo.toml" in names:
            _add_detected_directory(directories, languages, path, "rust")

    # Fetch root directory listing
    resp = await _github_request(
        "GET",
        _contents_url(),
        headers,
        max_retries=2,
        params=params,
    )
    if resp.status_code != 200:
        logger.warning(
            f"[ONBOARD] Failed to list repo contents for {owner}/{repo}: {resp.status_code}"
        )
        return {}

    root_listing = [item for item in resp.json() if isinstance(item, dict)]
    directories: list[Dict[str, Any]] = []
    languages: list[str] = []
    source_items: list[Dict[str, Any]] = list(root_listing)
    # Positive confirmation that every listing needed for the emptiness
    # determination was fetched. Any failed nested fetch, max-depth truncation,
    # or scan-cap truncation flips this to False so the repo is not falsely
    # classified as greenfield from a partial walk.
    listing_complete = True
    _detect_projects_in_listing("", root_listing, directories, languages)

    pending_dirs = deque(
        sorted(
            (item["name"], 1)
            for item in root_listing
            if item.get("type") == "dir" and isinstance(item.get("name"), str)
        )
    )
    scanned_directories = 0

    while pending_dirs:
        subdir, depth = pending_dirs.popleft()
        if scanned_directories >= _BUILD_CONFIG_SCAN_MAX_DIRECTORIES:
            logger.warning(
                "[ONBOARD] Stopped build-config scan for %s/%s after %s directories; "
                "treating repo read as incomplete",
                owner,
                repo,
                _BUILD_CONFIG_SCAN_MAX_DIRECTORIES,
            )
            listing_complete = False
            break
        sub_resp = await _github_request(
            "GET",
            _contents_url(subdir),
            headers,
            max_retries=1,
            params=params,
        )
        if sub_resp.status_code != 200:
            # We could not fully read this subdirectory. Record the incomplete
            # read so emptiness classification degrades to "not empty" rather
            # than silently concluding greenfield over a partial listing.
            logger.warning(
                f"[ONBOARD] Failed to list subdir {subdir} for {owner}/{repo}: "
                f"{sub_resp.status_code}; treating repo read as incomplete"
            )
            listing_complete = False
            continue

        sub_listing = [item for item in sub_resp.json() if isinstance(item, dict)]
        scanned_directories += 1
        source_items.extend(sub_listing)
        _detect_projects_in_listing(subdir, sub_listing, directories, languages)

        child_dirs = sorted(
            item["name"]
            for item in sub_listing
            if item.get("type") == "dir" and isinstance(item.get("name"), str)
        )
        if not child_dirs:
            continue
        if depth >= _BUILD_CONFIG_SCAN_MAX_DEPTH:
            listing_complete = False
            continue
        for child in child_dirs:
            pending_dirs.append((f"{subdir}/{child}", depth + 1))

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
        # Deterministic empty/near-empty signal from the already-fetched bounded
        # contents walk (zero extra GitHub cost beyond detection). Must stay
        # aligned with agent/src/prompts/discovery.ts and the threshold in
        # agent/src/constants.ts.
        "repo_is_empty": _classify_repo_emptiness(
            source_items, listing_complete=listing_complete
        ),
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

    token = None
    from app.github import get_repo_default_branch, get_repo_installation_token

    token = await get_repo_installation_token(payload.github_owner, payload.github_repo)
    if not token:
        logger.warning(
            "[ONBOARD] No GitHub token available for %s/%s",
            payload.github_owner,
            payload.github_repo,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No installation token available — cannot detect default branch.",
        )

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

    # Auto-detect build config from the same branch Blueprint stores as trunk.
    build_config: Dict[str, Any] = {}
    try:
        build_config = await _detect_build_config(
            token,
            payload.github_owner,
            payload.github_repo,
            ref=default_branch,
        )
    except Exception as e:
        logger.error(
            "[ONBOARD] Build config detection failed for %s/%s on ref %s: %s",
            payload.github_owner,
            payload.github_repo,
            default_branch,
            e,
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
        logger.warning(
            "[ONBOARD] build_config failed schema validation, storing raw: %s", exc
        )
        project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)

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
