"""Project onboarding endpoint with build config auto-detection.

The onboarding flow creates the project row, assigns owner membership, tries
to infer build commands from the GitHub repository root, seeds a stable
task-branch namespace, and then validates the merged configuration against the
BuildConfig schema before saving it.
"""

import base64
import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter
from app.models import (
    Project,
    ProjectMember,
    ProjectStatus,
    ProjectMessage,
    MessageType,
)
from app.auth import get_current_user
from app.ci_resolution import compute_merge_verification_verdict
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

# A real top-level ``test:`` target: line-anchored with NO leading whitespace,
# only optional whitespace allowed between ``test`` and the ``:``. ``make test``
# only ever runs an EXACT ``test`` target, so a prefixed target like
# ``test-integration:`` or an indented recipe line does NOT justify ``make test``
# (it fails with "No rule to make target 'test'"). The Contents API only gives
# file names, so a bare Makefile is not enough — we fetch the body and confirm
# this exact target exists. Mirrors the agent-side strict Makefile check
# (``textDefinesMakeTestTarget`` in agent/src/utils/test-runners.ts); note this
# is INTENTIONALLY stricter than the just/Taskfile check below, which tolerates
# a prefixed target name on both sides.
_MAKEFILE_TEST_TARGET_RE = re.compile(r"^test\s*:", re.MULTILINE)
_JUST_OR_TASK_TEST_TARGET_RE = re.compile(r"^\s*test\b[^:\n]*:", re.MULTILINE)

# Conventional root/dir-local test scripts, in deterministic precedence order,
# emitted as ``bash <script>`` (run from the directory the entry points at).
_CONVENTIONAL_TEST_SCRIPTS = ("test.sh", "run-tests.sh", "run_tests.sh")


def _json_defines_test_script(text: str | None) -> bool:
    """True iff a JSON manifest body declares a non-empty ``scripts.test``.

    Shared content-guard for the manifests whose test command is only runnable
    when the script is actually declared: a bare ``composer.json`` does not
    justify ``composer test`` ("Script test is not defined"), and a bare
    ``package.json`` does not justify ``npm test`` (npm's default "no test
    specified" placeholder runs no tests). The detector fetches the file
    CONTENT and confirms ``scripts.test`` is present and non-empty before
    emitting the command, mirroring the agent-side detector in
    ``agent/src/utils/test-runners.ts``.
    """
    if not text:
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return False
    return bool(str(scripts.get("test") or "").strip())


def _text_mentions_pytest(text: str | None) -> bool:
    """True iff the manifest body contains a real pytest signal."""
    return bool(text and "pytest" in text)


def _is_ci_workflow_file(name: str) -> bool:
    """True when a filename under ``.github/workflows`` is a GitHub Actions workflow.

    GitHub only executes ``*.yml`` / ``*.yaml`` files placed directly in
    ``.github/workflows`` as Actions workflows, so the presence of any such file
    is the deterministic signal that the repository has CI configured.
    """
    base = name.rsplit("/", 1)[-1].strip().lower()
    return base.endswith(".yml") or base.endswith(".yaml")


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


def _is_empty_repository_response(resp: Any) -> bool:
    """True when a contents listing failed *because the repo has zero commits*.

    GitHub answers ``GET /contents/`` on a commitless repo with
    404 ``{"message": "This repository is empty."}`` (and the Git Data endpoints
    answer 409 ``"Git Repository is empty"``). That is a permanent, meaningful
    state — not the transient GitHub failure a bare non-200 otherwise implies —
    and the two must not be conflated: treating it as transient is what let
    ``repo_is_empty`` default to False and spawned a bootstrap against a repo
    that can never be cloned.
    """
    if resp.status_code not in (404, 409):
        return False
    try:
        message = (resp.json() or {}).get("message", "")
    except Exception:  # noqa: BLE001 — non-JSON body: not the empty-repo signal
        return False
    return "repository is empty" in str(message).lower()


def _empty_repo_build_config() -> Dict[str, Any]:
    """The build config for a repository with no commits.

    Same shape a normal detection returns, so ``detection_completed`` is True
    and the ``repo_is_empty`` guard in ``repo_bootstrap`` is actually reachable.
    Nothing is asserted that we did not observe: there is no tree, therefore no
    language, no directories, and demonstrably no CI.
    """
    return {
        "language": None,
        "directories": [],
        "env_vars": {},
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "repo_is_empty": True,
        "source_file_count": 0,
        "has_ci": False,
    }


def _classify_repo_emptiness(
    root_items: list[Dict[str, Any]], listing_complete: bool = True
) -> tuple[bool | None, int | None]:
    """Classify a repo as empty / near-empty from fetched contents listings.

    Returns ``(is_empty, source_file_count)``.

    ``is_empty`` is tri-state, exactly like the ``has_ci`` signal derived from
    the same bounded walk. It is True ONLY when the repository was read
    successfully and completely AND has at most
    ``GREENFIELD_SOURCE_FILE_THRESHOLD`` real source files across the fetched
    root listing and every nested directory listing the detector walked; False
    when the walk completed and found more; and None (undetermined) when the
    read was incomplete. Documentation/config/data files, dotfiles, and common
    manifests are not counted as source.

    ``source_file_count`` is the authoritative real-source total from that same
    walk, persisted so the agent's live clone-time detector can compare
    like-for-like against this count rather than recomputing under its own,
    possibly divergent, exclusion rules. It is None when ``listing_complete`` is
    False (emptiness is indeterminate, so no count is asserted).

    ``listing_complete`` is the positive confirmation that every listing needed
    to make this determination was fetched. When it is False (e.g. a nested
    directory listing failed to fetch or the bounded scan hit unread deeper
    paths), emptiness is indeterminate and this returns ``(None, None)``
    (undetermined, NOT asserted-non-empty) — a repo whose real source lives in a
    subdirectory we could not read must never be recorded as a durable,
    authoritative "not empty" on a transient fetch error, and a genuinely
    greenfield repo whose listing hit a transient failure must not be suppressed
    out of greenfield mode by a false negative.

    ``root_items`` is the raw GitHub ``contents/`` listings already fetched by
    :func:`_detect_build_config`, so it adds zero GitHub cost.
    """
    if not listing_complete:
        return None, None

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

    return source_file_count <= GREENFIELD_SOURCE_FILE_THRESHOLD, source_file_count


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
        python_has_test_signal: bool = True,
        node_has_test_script: bool = False,
    ) -> None:
        normalized_path = _normalized_directory_path(path)
        existing = next(
            (
                d
                for d in directories
                if d["path"] == normalized_path and d.get("type") == project_type
            ),
            None,
        )
        if existing is not None:
            if project_type == "python" and python_has_test_signal:
                existing["test_cmd"] = "python3 -m pytest"
            if project_type == "node" and node_has_test_script:
                existing["test_cmd"] = "npm test"
            return

        if project_type == "python":
            entry: Dict[str, Any] = {
                "path": normalized_path,
                "type": "python",
                "lint_cmd": "ruff check",
            }
            # Only persist a test command the runtime can really honor. For a
            # pyproject-only directory whose body does not configure pytest the
            # caller passes python_has_test_signal=False, so the dir still
            # records the python language + lint_cmd without a false "tests
            # gated" promise.
            if python_has_test_signal:
                entry["test_cmd"] = "python3 -m pytest"
            directories.append(entry)
            _record_language(languages, "python")
            return

        if project_type == "node":
            entry = {
                "path": normalized_path,
                "type": "node",
                "install_cmd": "npm ci",
                "build_cmd": "npm run build",
            }
            # Only gate the merge as "tests gated" when package.json actually
            # declares a non-empty ``scripts.test`` — a bare package.json runs
            # npm's "no test specified" placeholder, which executes no tests.
            # Mirrors the composer.json / Makefile content-guards and the
            # agent-side detector in agent/src/utils/test-runners.ts.
            if node_has_test_script:
                entry["test_cmd"] = "npm test"
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

    async def _detect_projects_in_listing(
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

        # Python: pytest.ini is the one unconditional pytest signal (matching the
        # runtime test-runner detector). A pyproject.toml- or setup.cfg-only
        # directory emits a test command only when its body actually configures
        # pytest, fetched and confirmed before emission so the stored config
        # never promises a suite the runtime would resolve to no-tests. A bare
        # setup.py is a packaging signal only — the runtime emits no runner for
        # it, so neither does onboarding (keeps no-CI parity).
        if {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini"} & names:
            python_has_test_signal = "pytest.ini" in names
            if not python_has_test_signal and "pyproject.toml" in names:
                pyproject_path = f"{path}/pyproject.toml" if path else "pyproject.toml"
                python_has_test_signal = _text_mentions_pytest(
                    await _fetch_file_text(pyproject_path)
                )
            if not python_has_test_signal and "setup.cfg" in names:
                setup_cfg_path = f"{path}/setup.cfg" if path else "setup.cfg"
                python_has_test_signal = _text_mentions_pytest(
                    await _fetch_file_text(setup_cfg_path)
                )
            _add_detected_directory(
                directories,
                languages,
                path,
                "python",
                python_has_test_signal=python_has_test_signal,
            )
        # Node: ``npm test`` only when package.json declares scripts.test, which
        # is what the runtime honors; an absent test script must fall back, not
        # block, so the dir is still recorded but carries no test command.
        if "package.json" in names:
            # Fetch + inspect package.json so ``npm test`` is only emitted when a
            # real ``scripts.test`` exists (otherwise has_detectable_tests would
            # claim "tests gated" for a package with no runnable suite).
            package_path = f"{path}/package.json" if path else "package.json"
            node_has_test_script = _json_defines_test_script(
                await _fetch_file_text(package_path)
            )
            _add_detected_directory(
                directories,
                languages,
                path,
                "node",
                node_has_test_script=node_has_test_script,
            )
        if "go.mod" in names:
            _add_detected_directory(directories, languages, path, "go")
        if "Cargo.toml" in names:
            _add_detected_directory(directories, languages, path, "rust")

    def _directory_has_test_cmd(path: str) -> bool:
        normalized_path = _normalized_directory_path(path)
        return any(
            d["path"] == normalized_path and str(d.get("test_cmd") or "").strip()
            for d in directories
        )

    def _add_fallback_directory(path: str, test_cmd: str) -> None:
        """Append a generic ``type='other'`` directory carrying ``test_cmd``.

        Deduped on (path, test_cmd). Intentionally does NOT record a language —
        the top-level ``BuildConfig.language`` only models
        python/typescript/go/rust/mixed, so a non-core entrypoint leaves the
        summary language unchanged rather than emitting an out-of-pattern value
        the schema would reject.
        """
        normalized_path = _normalized_directory_path(path)
        if any(
            d["path"] == normalized_path and d.get("test_cmd") == test_cmd
            for d in directories
        ):
            return
        directories.append(
            {"path": normalized_path, "type": "other", "test_cmd": test_cmd}
        )

    async def _fetch_file_text(file_path: str) -> str | None:
        """Fetch + decode a single repo file's text.

        Prefers the cached snapshot layer (app.github_repo_snapshot) when a
        branch ref is known — collapses repeated file fetches to ≤1 blob call
        per unique file per TTL window.  Falls back to the direct GitHub
        Contents API when the snapshot is unavailable (quota reserve, network
        error, or no ref supplied).

        Returns the decoded text, or None on any failure — callers must treat
        None as 'could not confirm' and emit no command.
        """
        if ref is not None:
            from app.github_repo_snapshot import file_text_on_branch as _fton

            _exists, _text = await _fton(
                file_path,
                token=token,
                owner=owner,
                repo=repo,
                branch=ref,
            )
            if _text is not None:
                return _text
            if _exists is False:
                # Definitively absent on this branch.
                return None
            # _exists is None (snapshot unavailable) or True but blob unreadable;
            # fall through to direct API.

        resp = await _github_request(
            "GET",
            _contents_url(file_path),
            headers,
            max_retries=1,
            params=params,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("type") != "file":
            return None
        content_b64 = payload.get("content")
        if not isinstance(content_b64, str):
            return None
        try:
            return base64.b64decode(content_b64).decode(errors="replace")
        except Exception:  # noqa: BLE001 — undecodable content → cannot confirm
            return None

    async def _detect_fallback_in_listing(
        path: str, listing: list[Dict[str, Any]]
    ) -> None:
        """Detect non-manifest / additional-language test entrypoints in a dir.

        Runs AFTER the core four-manifest detection for the same directory and
        only when that directory has no recognized-manifest ``test_cmd`` yet, so
        the existing python/node/go/rust behavior always takes precedence. Emits
        at most one ``type='other'`` directory carrying a runnable ``test_cmd``
        so the no-CI merge gate's ``has_detectable_tests`` becomes true for repos
        whose tests run via make/tox/nox/a root script or a language outside the
        core four (Ruby, JVM, Elixir, .NET, PHP).

        Mirrors the agent-side fallback detector in
        ``agent/src/utils/test-runners.ts`` so both layers agree on the emitted
        command. SAFETY: a Makefile must expose a real ``test:`` target and a
        ``composer.json`` must declare ``scripts.test``; those are confirmed by
        fetching the file CONTENT before a command is emitted.
        """
        if _directory_has_test_cmd(path):
            return

        names = {
            item.get("name")
            for item in listing
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }

        # --- Additional language manifests (name-based). First match wins. ---
        if "Gemfile" in names and "Rakefile" in names:
            _add_fallback_directory(path, "bundle exec rake test")
            return
        if "build.gradle" in names or "build.gradle.kts" in names:
            _add_fallback_directory(path, "./gradlew test")
            return
        if "pom.xml" in names:
            _add_fallback_directory(path, "mvn test")
            return
        if "mix.exs" in names:
            _add_fallback_directory(path, "mix test")
            return
        if any(n.endswith(".sln") or n.endswith(".csproj") for n in names):
            _add_fallback_directory(path, "dotnet test")
            return
        if "phpunit.xml" in names or "phpunit.xml.dist" in names:
            _add_fallback_directory(path, "vendor/bin/phpunit")
            return
        if "composer.json" in names:
            composer_path = f"{path}/composer.json" if path else "composer.json"
            if _json_defines_test_script(await _fetch_file_text(composer_path)):
                _add_fallback_directory(path, "composer test")
                return
        if "Package.swift" in names:
            _add_fallback_directory(path, "swift test")
            return
        if "build.sbt" in names:
            _add_fallback_directory(path, "sbt test")
            return
        if "WORKSPACE" in names or "WORKSPACE.bazel" in names:
            _add_fallback_directory(path, "bazel test //...")
            return
        if any(n.endswith(".cabal") for n in names):
            _add_fallback_directory(path, "cabal test all")
            return
        if "pubspec.yaml" in names:
            _add_fallback_directory(path, "flutter test")
            return

        # --- Generic non-manifest fallbacks: Makefile → tox → nox → script. ---
        for makefile_name in ("Makefile", "makefile", "GNUmakefile"):
            if makefile_name in names:
                makefile_path = f"{path}/{makefile_name}" if path else makefile_name
                text = await _fetch_file_text(makefile_path)
                if text and _MAKEFILE_TEST_TARGET_RE.search(text):
                    _add_fallback_directory(path, "make test")
                    return
        for justfile_name in ("Justfile", "justfile"):
            if justfile_name in names:
                justfile_path = f"{path}/{justfile_name}" if path else justfile_name
                text = await _fetch_file_text(justfile_path)
                if text and _JUST_OR_TASK_TEST_TARGET_RE.search(text):
                    _add_fallback_directory(path, "just test")
                    return
        for taskfile_name in ("Taskfile.yml", "Taskfile.yaml"):
            if taskfile_name in names:
                taskfile_path = f"{path}/{taskfile_name}" if path else taskfile_name
                text = await _fetch_file_text(taskfile_path)
                if text and _JUST_OR_TASK_TEST_TARGET_RE.search(text):
                    _add_fallback_directory(path, "task test")
                    return
        if "tox.ini" in names:
            _add_fallback_directory(path, "tox")
            return
        if "noxfile.py" in names:
            _add_fallback_directory(path, "nox")
            return
        for script in _CONVENTIONAL_TEST_SCRIPTS:
            if script in names:
                if path == "scripts" and script == "test.sh":
                    _add_fallback_directory("", "bash scripts/test.sh")
                else:
                    _add_fallback_directory(path, f"bash {script}")
                return

    # Fetch root directory listing — prefer the cached snapshot layer (one
    # recursive git/trees call) over per-directory /contents requests.
    root_listing: list[Dict[str, Any]] = []
    _root_from_snapshot = False
    if ref is not None:
        from app.github_repo_snapshot import list_dir_children_on_branch as _ldcb

        _snap_root = await _ldcb("", token=token, owner=owner, repo=repo, branch=ref)
        if _snap_root is not None:
            root_listing = _snap_root
            _root_from_snapshot = True

    if not _root_from_snapshot:
        resp = await _github_request(
            "GET",
            _contents_url(),
            headers,
            max_retries=2,
            params=params,
        )
        if resp.status_code != 200:
            if _is_empty_repository_response(resp):
                logger.info(
                    f"[ONBOARD] {owner}/{repo} has no commits — detected as an empty repo"
                )
                return _empty_repo_build_config()
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
    # Whether the bounded walk positively observed a GitHub Actions workflow
    # file under ``.github/workflows``. ``.github`` is a root dir (depth 1) and
    # ``.github/workflows`` is reached at depth 2, within the scan bounds, so a
    # normal walk reads it for free alongside build-config detection.
    ci_workflow_found = False
    await _detect_projects_in_listing("", root_listing, directories, languages)
    await _detect_fallback_in_listing("", root_listing)

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
        # Prefer the snapshot layer when the root was also read via snapshot
        # (guarantees the same tree version for all directory listings).
        sub_listing: list[Dict[str, Any]] | None = None
        if _root_from_snapshot and ref is not None:
            from app.github_repo_snapshot import list_dir_children_on_branch as _ldcb

            sub_listing = await _ldcb(
                subdir, token=token, owner=owner, repo=repo, branch=ref
            )

        if sub_listing is None:
            sub_resp = await _github_request(
                "GET",
                _contents_url(subdir),
                headers,
                max_retries=1,
                params=params,
            )
            if sub_resp.status_code != 200:
                # We could not fully read this subdirectory. Record the
                # incomplete read so emptiness classification degrades to
                # "not empty" rather than silently concluding greenfield
                # over a partial listing.
                logger.warning(
                    f"[ONBOARD] Failed to list subdir {subdir} for {owner}/{repo}: "
                    f"{sub_resp.status_code}; treating repo read as incomplete"
                )
                listing_complete = False
                continue
            sub_listing = [item for item in sub_resp.json() if isinstance(item, dict)]
        scanned_directories += 1
        source_items.extend(sub_listing)
        if subdir == ".github/workflows" and any(
            item.get("type") == "file"
            and isinstance(item.get("name"), str)
            and _is_ci_workflow_file(item["name"])
            for item in sub_listing
        ):
            ci_workflow_found = True
        await _detect_projects_in_listing(subdir, sub_listing, directories, languages)
        await _detect_fallback_in_listing(subdir, sub_listing)

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

    repo_is_empty, source_file_count = _classify_repo_emptiness(
        source_items, listing_complete=listing_complete
    )
    config: Dict[str, Any] = {
        "language": language,
        "directories": directories,
        "env_vars": {},
        "detected_at": datetime.now(timezone.utc).isoformat(),
        # Tri-state empty/near-empty signal from the already-fetched bounded
        # contents walk (zero extra GitHub cost beyond detection): True when the
        # walk completed and found at most the greenfield threshold of real
        # source; False when it completed and found more; None (undetermined)
        # when the read was incomplete, so emptiness is never asserted from a
        # partial walk (parity with ``has_ci`` below). ``exclude_none`` drops the
        # key when None so an undetermined walk persists as absent rather than a
        # durable False. Must stay aligned with agent/src/prompts/discovery.ts
        # and the threshold in agent/src/constants.ts.
        "repo_is_empty": repo_is_empty,
        # The authoritative real-source count from the same walk, persisted so
        # the agent's clone-time detector compares against this number instead of
        # recomputing under its own exclusion rules. None when the read was
        # incomplete (emptiness indeterminate).
        "source_file_count": source_file_count,
    }

    # Tri-state CI-presence signal derived from the same bounded walk (zero
    # extra GitHub cost): True when a workflow file was observed; False when the
    # walk completed without finding one; None (undetermined) when the read was
    # incomplete, so the absence of CI is never asserted from a partial walk.
    if ci_workflow_found:
        config["has_ci"] = True
    elif listing_complete:
        config["has_ci"] = False
    else:
        config["has_ci"] = None

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
    current_user: dict = Depends(get_current_user),
):
    """Create a new project, seed membership, and persist detected build config.

    Any allowlisted (authenticated, email-verified) user may onboard a project;
    they are recorded as its ``owner`` via ``ProjectMember`` below and all
    project-scoped reads (e.g. ``GET /projects``) are filtered by that
    membership, so ownership — not platform-admin role — is what grants access.
    Gating this on ``require_admin`` dead-ended first-run onboarding: a freshly
    allowlisted user defaults to ``role="user"`` (see ``AllowedUser`` /
    ``_UserCreate``), lands on the WelcomeScreen, and its "Create your first
    project" action 403'd here even though the rest of the stack supports
    non-admin ownership. The allowlist itself remains the access boundary.

    Flow:
    1. Create the Project row in DEPLOYING state.
    2. Add the caller as owner via ProjectMember.
    3. Attempt GitHub-based build-config detection.
    4. Seed ``task_branch_namespace`` from the repo name when missing.
    5. Apply any caller-provided build_config_override on top of detection.
    6. Validate against ``BuildConfig`` and store the validated shape when
       possible, or the raw detected dict if validation fails.
    """

    token = None
    from app.github import (
        ensure_repo_initialized,
        get_repo_default_branch,
        get_repo_installation_token,
    )

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

    # A repo with zero commits reports a default_branch that does not exist, so
    # the trunk we are about to store would be a phantom: every clone in the
    # fleet passes `--branch <trunk>` and would die with "Remote branch not
    # found in upstream origin", starting with the repo_bootstrap task that is
    # supposed to seed this very project. Give it its first commit now, while we
    # still hold a write-capable installation token, so the trunk is real before
    # anything tries to clone it. No-op on a repo that already has commits.
    #
    # Failing loudly here is deliberate: an un-seeded empty repo produces a
    # project that can never run a single task, and a 502 at onboarding is far
    # cheaper to diagnose than a bootstrap that retries forever.
    try:
        await ensure_repo_initialized(
            token,
            payload.github_owner,
            payload.github_repo,
            default_branch,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[ONBOARD] Empty-repo initialization failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Empty-repo initialization failed: {e}",
        ) from e

    # Auto-detect build config from the same branch Blueprint stores as trunk.
    build_config: Dict[str, Any] = {}
    detection_completed = False
    try:
        build_config = await _detect_build_config(
            token,
            payload.github_owner,
            payload.github_repo,
            ref=default_branch,
        )
        # A successful detection always returns a non-empty dict (it carries
        # detected_at/repo_is_empty/has_ci); an empty/falsy result means the
        # repo-contents listing came back non-200 (transient GitHub failure).
        detection_completed = bool(build_config)
    except Exception as e:
        logger.error(
            "[ONBOARD] Build config detection failed for %s/%s on ref %s: %s",
            payload.github_owner,
            payload.github_repo,
            default_branch,
            e,
            exc_info=True,
        )

    # When detection did not complete (transient GitHub 5xx/rate-limit/network
    # blip or a non-200 listing), persist a durable marker so the idempotent
    # landing-time auto-refresh re-detects this repo once GitHub is reachable
    # again — instead of leaving a no-CI repo permanently un-gated. A detection
    # that completed and genuinely found no tests is NOT flagged (it set
    # repo_is_empty/has_ci), so a legitimately test-less repo is never re-scanned
    # on every landing.
    if not detection_completed:
        build_config["detection_incomplete"] = True

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
        build_config = validated.model_dump(exclude_none=True)
    except Exception as exc:  # noqa: BLE001
        # Detection + override produced invalid shape — store raw but log warning
        logger.warning(
            "[ONBOARD] build_config failed schema validation, storing raw: %s", exc
        )

    response = {
        "build_config": build_config,
        "default_branch": default_branch,
        "merge_verification_verdict": compute_merge_verification_verdict(
            SimpleNamespace(build_config=build_config)
        ),
    }
    if payload.preview_only:
        return response

    # Create project record only after the preview path has succeeded, so a
    # preview-only request never touches the database.
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

    project.integration_branch = default_branch
    project.promotion_target = default_branch

    if payload.promotion_target:
        project.promotion_target = payload.promotion_target

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

    # Auto-spawn a repo_bootstrap task for non-self-repo, non-empty projects
    # that have not already been bootstrapped. This runs once before the first
    # strategic cycle to seed design doc sections and confirm build/test commands.
    try:
        from app.repo_bootstrap import spawn_bootstrap_task

        _result = await spawn_bootstrap_task(
            project,
            session,
            is_first_bootstrap=True,
            spawn_reason="first_cycle",
        )
        if _result.outcome == "spawned" or _result.build_config_changed:
            await session.commit()
            if _result.outcome == "spawned":
                logger.info(
                    "[ONBOARD] Spawned repo_bootstrap task %s for project %s",
                    _result.task_id,
                    project.id,
                )
    except Exception:
        logger.exception(
            "[ONBOARD] Failed to spawn repo_bootstrap task for project %s — continuing",
            project.id,
        )

    response["project_id"] = str(project.id)
    response["build_config"] = project.build_config or build_config or {}
    response["merge_verification_verdict"] = compute_merge_verification_verdict(project)

    # Enable the warm pool for this project inline when the caller requests it.
    # Runs after the final commit so project.id is durable. A failure here never
    # aborts the onboard — the caller gets a warning key instead.
    if not payload.preview_only and payload.warm_pool_target_size is not None:
        try:
            from app.routers.admin_settings import _apply_settings
            from app.shared_state import runtime_config, runtime_config_lock
            from app.warm_pool import (
                warm_pool_project_scope,
                warm_pool_project_target_map,
            )
            from app.routers.admin_warm_pool import _seed_project_pool

            async with runtime_config_lock:
                scope = warm_pool_project_scope()
                scope.add(str(project.id))
                new_target_map = dict(warm_pool_project_target_map())
                new_target_map[str(project.id)] = payload.warm_pool_target_size
                await _apply_settings(
                    {
                        "warm_pool_enabled": True,
                        "warm_pool_project_allowlist": sorted(scope),
                        "warm_pool_target_size_by_project": new_target_map,
                    },
                    session,
                    runtime_config,
                )

            logger.info(
                "[ONBOARD] Enabled warm pool project=%s target=%d by onboard",
                project.id,
                payload.warm_pool_target_size,
            )
            seed_summary = await _seed_project_pool(
                session, project.id, current_user["user_id"]
            )
            response["warm_pool"] = {
                **seed_summary,
                "enabled": True,
                "target_size": payload.warm_pool_target_size,
            }
        except Exception:
            logger.exception(
                "[ONBOARD] Failed to enable warm pool for project %s — continuing",
                project.id,
            )
            response["warm_pool"] = {"enabled": False, "error": "setup_failed"}

    return response
