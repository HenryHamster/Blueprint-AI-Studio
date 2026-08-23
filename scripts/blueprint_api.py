"""Blueprint orchestrator API helper.

Shared module for all admin scripts. Reads auth from .blueprint-config.json.
Never hardcodes secrets, repos, or task IDs.

Usage as library:
    from blueprint_api import api, config
    tasks = api("GET", "/api/admin/tasks?limit=10")
    api("PATCH", f"/api/admin/tasks/{task_id}", {"status": "QUEUED"})

Usage as CLI:
    python scripts/blueprint_api.py create_task "title" "prompt" --type review --pr-number 84
    python scripts/blueprint_api.py list_tasks --status EVALUATING --limit 20
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

CONFIG_FILE = ".blueprint-config.json"
SCOPED_PROJECT_PAGE_LIMIT = 50
SCOPED_PROJECT_CATALOG_MAX = 10_000

_config_cache: dict | None = None


class APIError(RuntimeError):
    """Raised by api() on HTTP or network errors; caught by main() to exit cleanly."""


def _config_search_paths() -> list[Path]:
    """Config-file locations checked in priority order (keeps secrets out of the repo)."""
    paths: list[Path] = []
    explicit = os.environ.get("BLUEPRINT_CONFIG")
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(Path(__file__).resolve().parent.parent / CONFIG_FILE)  # repo root
    paths.append(Path.home() / CONFIG_FILE)                             # ~/.blueprint-config.json
    return paths


# ── gcloud bootstrap fallback ───────────────────────────────────────────────
# If no config file/env is found, regenerate it from Cloud Run + Secret Manager
# on any machine with `gcloud auth login`. Overridable per-deployment via env so
# this isn't hardcoded to one project. The secret is cached only to the home-dir
# config file — never echoed, never committed.
GCP_SERVICE = os.environ.get("BLUEPRINT_GCP_SERVICE", "blueprint-orchestrator")
GCP_REGION = os.environ.get("BLUEPRINT_GCP_REGION", "us-central1")
GCP_API_KEY_SECRET = os.environ.get("BLUEPRINT_API_KEY_SECRET", "ADMIN_API_KEY")
DEFAULT_REPO = os.environ.get("BLUEPRINT_DEFAULT_REPO", "HenryHamster/BlueprintAIStudio")
DEFAULT_BASE_BRANCH = os.environ.get("BLUEPRINT_DEFAULT_BASE_BRANCH", "main")


def _gcloud(*args: str) -> str | None:
    """Run a read-only gcloud command; return stripped stdout, or None on any failure.

    Resolves the executable via shutil.which (finds gcloud.cmd on Windows) and
    runs .cmd/.bat shims through the shell so CreateProcess can launch them.
    """
    exe = shutil.which("gcloud")
    if not exe:
        return None
    use_shell = exe.lower().endswith((".cmd", ".bat"))
    cmd = subprocess.list2cmdline([exe, *args]) if use_shell else [exe, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, shell=use_shell)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _bootstrap_from_gcloud() -> dict | None:
    """Last resort: rebuild config from Cloud Run + Secret Manager and cache it to ~/.

    Requires an authenticated gcloud (the same boundary that protects the
    secret). Returns None when gcloud is unavailable/unauthed so callers fall
    through to guidance.
    """
    api_key = _gcloud("secrets", "versions", "access", "latest", f"--secret={GCP_API_KEY_SECRET}")
    if not api_key:
        return None
    api_url = _gcloud("run", "services", "describe", GCP_SERVICE,
                      f"--region={GCP_REGION}", "--format=value(status.url)")
    if not api_url:
        return None
    cfg = {"api_url": api_url, "api_key": api_key,
           "default_repo": DEFAULT_REPO, "default_base_branch": DEFAULT_BASE_BRANCH}
    home_path = Path.home() / CONFIG_FILE
    try:
        home_path.write_text(json.dumps(cfg, indent=2) + "\n")
        try:
            home_path.chmod(0o600)
        except OSError:
            pass
        print(f"[blueprint_api] bootstrapped config from gcloud -> {home_path}", file=sys.stderr)
    except OSError:
        pass  # usable in-memory even if we cannot cache it
    return cfg


def load_config() -> dict:
    """Load API config. Explicit config/env credentials beat ambient files.

    Search order is ``$BLUEPRINT_CONFIG`` (when it names an existing file),
    ``BLUEPRINT_API_URL`` + ``BLUEPRINT_API_KEY``, the repo-root config, then
    the home config. Environment credentials must win over ambient files so a
    coding-agent process can deliberately select a scoped service credential
    even when an operator has a broader local config.

    Read-only fallback: if all admin-credential tiers fail but ORCHESTRATOR_URL
    (or BLUEPRINT_API_URL) and AGENT_SECRET_TOKEN are set, returns a read-only
    config using the agent JWT as the bearer credential. This path grants access
    only to non-mutating operator-metric endpoints (e.g. /api/admin/digest-latency-stats);
    mutating/admin endpoints will 403. The returned dict carries ``read_only: True``.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    config_paths = _config_search_paths()
    explicit_config = os.environ.get("BLUEPRINT_CONFIG")
    if explicit_config:
        explicit_path = Path(explicit_config).expanduser()
        if not explicit_path.exists():
            raise APIError(f"BLUEPRINT_CONFIG does not exist: {explicit_path}")
        _config_cache = json.loads(explicit_path.read_text())
        return _config_cache
    env_url, env_key = os.environ.get("BLUEPRINT_API_URL"), os.environ.get("BLUEPRINT_API_KEY")
    if env_url and env_key:
        _config_cache = {
            "api_url": env_url,
            "api_key": env_key,
            "default_repo": os.environ.get("BLUEPRINT_DEFAULT_REPO", ""),
            "default_base_branch": os.environ.get("BLUEPRINT_DEFAULT_BASE_BRANCH", "main"),
        }
        return _config_cache
    for config_path in config_paths:
        if config_path.exists():
            _config_cache = json.loads(config_path.read_text())
            return _config_cache
    bootstrapped = _bootstrap_from_gcloud()
    if bootstrapped is not None:
        _config_cache = bootstrapped
        return _config_cache
    # Read-only agent-JWT fallback: research/measurement VMs have ORCHESTRATOR_URL
    # and AGENT_SECRET_TOKEN but no admin key or config file. This path works only
    # for read-only metric endpoints (e.g. /api/admin/digest-latency-stats) — the
    # backend accepts a valid agent JWT on those endpoints via get_metrics_reader.
    agent_token = os.environ.get("AGENT_SECRET_TOKEN")
    orchestrator_url = os.environ.get("ORCHESTRATOR_URL") or env_url
    if agent_token and orchestrator_url:
        _config_cache = {
            "api_url": orchestrator_url,
            "api_key": agent_token,
            "default_repo": os.environ.get("BLUEPRINT_DEFAULT_REPO", ""),
            "default_base_branch": os.environ.get("BLUEPRINT_DEFAULT_BASE_BRANCH", "main"),
            "read_only": True,
        }
        return _config_cache
    searched = "\n  ".join(str(p) for p in _config_search_paths())
    message = (
        "ERROR: no Blueprint API config found. Searched:\n  " + searched + "\n"
        "Fix with any of:\n"
        "  - run `gcloud auth login` (config auto-bootstraps from Secret Manager), or\n"
        "  - create ~/.blueprint-config.json "
        "(keys: api_url, api_key, default_repo, default_base_branch), or\n"
        "  - export BLUEPRINT_API_URL + BLUEPRINT_API_KEY, or\n"
        "  - export ORCHESTRATOR_URL + AGENT_SECRET_TOKEN "
        "(read-only metric access only)."
    )
    print(message, file=sys.stderr)
    raise APIError("no Blueprint API config found")


def api(method: str, path: str, body: dict | None = None, timeout: int = 30) -> Any:
    """Make an authenticated request to the orchestrator API. Returns parsed JSON."""
    cfg = load_config()
    url = cfg["api_url"].rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        if e.code == 401:
            print(
                "HTTP 401 Unauthorized — your credential is invalid or expired.\n"
                "  If using AGENT_SECRET_TOKEN, the token may have expired; "
                "re-authenticate or request a new token.\n"
                "  If using BLUEPRINT_API_KEY or .blueprint-config.json, "
                "verify the key is correct.",
                file=sys.stderr,
            )
        elif e.code == 403:
            if str(cfg.get("api_key") or "").startswith("bpsc_"):
                print(
                    "HTTP 403 Forbidden — this scoped ServiceCredential does not "
                    f"permit {method.upper()} {path}. Select an explicitly authorized "
                    "project-operator credential for supported project actions; "
                    "platform-admin commands remain unavailable.",
                    file=sys.stderr,
                )
            elif cfg.get("read_only"):
                print(
                    "HTTP 403 Forbidden — the current config is read-only. Select "
                    "a separate operator BLUEPRINT_CONFIG for authorized mutations.",
                    file=sys.stderr,
                )
        print(f"API error {e.code}: {body}", file=sys.stderr)
        raise APIError(f"HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        print(f"Error: cannot reach API at {url}: {e.reason}", file=sys.stderr)
        raise APIError(f"cannot reach API at {url}: {e.reason}")


# ── Convenience helpers ─────────────────────────────────────────────────────

def list_tasks(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """Fetch tasks, optionally filtered by status."""
    path = f"/api/admin/tasks?limit={limit}&offset={offset}"
    if status:
        path += f"&status={status}"
    data = api("GET", path)
    return data.get("tasks", data) if isinstance(data, dict) else data


def all_tasks_by_status(*statuses: str, limit: int = 500) -> list[dict]:
    """Fetch all tasks across multiple statuses."""
    result = []
    for status in statuses:
        result.extend(list_tasks(status=status, limit=limit))
    return result


def patch_task(task_id: str, payload: dict) -> dict:
    """Update a task via PATCH."""
    return api("PATCH", f"/api/admin/tasks/{task_id}", payload)


def create_task(
    title: str,
    prompt: str,
    task_type: str = "create",
    base_branch: str | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
) -> dict:
    """Submit a new task to the orchestrator."""
    cfg = load_config()
    payload = {
        "github_repo": cfg["default_repo"],
        "title": title,
        "prompt": prompt,
        "base_branch": base_branch or cfg.get("default_base_branch", "main"),
        "task_type": task_type,
    }
    if pr_number is not None:
        payload["pr_number"] = pr_number
    if branch is not None:
        payload["branch"] = branch
    return api("POST", "/api/admin/tasks", payload)


_project_id_cache: str | None = None


def get_project_id(project_id: str | None = None) -> str:
    """Return a project ID.

    Precedence (first non-empty wins):
      1. explicit ``project_id`` arg — from the ``--project-id`` / ``--project``
         CLI flags, already resolved to an id by the caller;
      2. the ``BLUEPRINT_PROJECT_ID`` env var;
      3. ``project_id`` configured in the config file;
      4. the unique project whose GitHub repo matches default_repo;
      5. the only project returned by the API.

    Previously (1) was honored but (2) was silently dropped and it fell straight
    to (4), so an explicit env selection was ignored and the CLI targeted an
    unrelated first project.
    """
    global _project_id_cache
    if project_id:
        return project_id
    env_pid = os.environ.get("BLUEPRINT_PROJECT_ID")
    if env_pid:
        return env_pid
    cfg = load_config()
    if cfg.get("project_id"):
        return cfg["project_id"]
    if _project_id_cache:
        return _project_id_cache
    projects = list_projects()
    if not projects:
        print("ERROR: No projects found.")
        sys.exit(1)

    default_repo = str(cfg.get("default_repo") or "").strip().casefold()
    if default_repo:
        repo_matches = []
        for candidate in projects:
            owner = str(candidate.get("github_owner") or "").strip()
            repo = str(candidate.get("github_repo") or "").strip()
            qualified = f"{owner}/{repo}".casefold() if owner and repo else ""
            if default_repo == qualified or (
                "/" not in default_repo and default_repo == repo.casefold()
            ):
                repo_matches.append(candidate)
        if len(repo_matches) == 1:
            _project_id_cache = repo_matches[0]["id"]
            return _project_id_cache
        if len(repo_matches) > 1:
            names = ", ".join(
                str(candidate.get("name") or candidate.get("id") or "?")
                for candidate in repo_matches
            )
            print(
                f"ERROR: default_repo {cfg.get('default_repo')!r} matches "
                f"multiple projects: {names}. Set project_id in "
                f"{CONFIG_FILE} or BLUEPRINT_PROJECT_ID explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)

    if len(projects) > 1:
        names = ", ".join(
            str(candidate.get("name") or candidate.get("id") or "?")
            for candidate in projects
        )
        print(
            "ERROR: no default project is configured and multiple projects are "
            f"available: {names}. Set project_id in "
            f"{CONFIG_FILE}, BLUEPRINT_PROJECT_ID, or pass --project-id.",
            file=sys.stderr,
        )
        sys.exit(1)
    _project_id_cache = projects[0]["id"]
    return _project_id_cache


def list_projects() -> list[dict]:
    """Return all projects (each dict carries at least ``id`` and ``name``)."""
    config = load_config()
    if str(config.get("api_key") or "").startswith("bpsc_"):
        projects: list[dict] = []
        offset = 0
        while True:
            profile = api(
                "GET",
                "/api/agent-access/v1/me"
                f"?limit={SCOPED_PROJECT_PAGE_LIMIT}&offset={offset}",
            )
            if not isinstance(profile, dict) or not isinstance(
                profile.get("projects"), list
            ):
                raise APIError("Agent access profile did not return a project list")
            page = profile["projects"]
            projects.extend(page)
            if not profile.get("has_more"):
                return projects
            if not page:
                raise APIError(
                    "Agent access profile reported has_more=true but returned "
                    "no projects"
                )
            if len(projects) >= SCOPED_PROJECT_CATALOG_MAX:
                raise APIError(
                    "Scoped project catalog exceeds the bounded CLI lookup limit; "
                    "set BLUEPRINT_PROJECT_ID or project_id explicitly"
                )
            next_offset = profile.get("next_offset", offset + len(page))
            if (
                isinstance(next_offset, bool)
                or not isinstance(next_offset, int)
                or next_offset <= offset
                or next_offset > SCOPED_PROJECT_CATALOG_MAX
            ):
                raise APIError(
                    "Agent access profile returned an invalid next project offset"
                )
            offset = next_offset

    projects: list[dict] = []
    offset = 0
    limit = 500
    while True:
        resp = api(
            "GET",
            f"/api/admin/projects?limit={limit}&offset={offset}",
        )
        if not isinstance(resp, dict):
            return resp or []

        page = resp.get("items") or []
        projects.extend(page)
        if not resp.get("has_more"):
            return projects
        if not page:
            raise APIError(
                "Project listing reported has_more=true but returned no items"
            )
        offset += len(page)


def resolve_project_id_by_name(name: str) -> str:
    """Resolve a project id from a case-insensitive substring match on its name.

    An exact (case-insensitive) name match wins outright so that a name which is
    also a substring of another (e.g. "Blueprint" vs "Blueprint Staging") is not
    reported as ambiguous. Otherwise exactly one substring match must remain;
    zero or more than one exits with a clear message rather than silently
    targeting the wrong project.
    """
    needle = name.strip().lower()
    projects = list_projects()
    exact = [p for p in projects if (p.get("name") or "").lower() == needle]
    if len(exact) == 1:
        return exact[0]["id"]
    matches = [p for p in projects if needle in (p.get("name") or "").lower()]
    if not matches:
        available = ", ".join(sorted((p.get("name") or "?") for p in projects)) or "(none)"
        print(f"ERROR: no project matches --project '{name}'. Available: {available}",
              file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        names = ", ".join((p.get("name") or "?") for p in matches)
        print(f"ERROR: --project '{name}' is ambiguous; matches: {names}. "
              "Narrow the name or use --project-id.", file=sys.stderr)
        sys.exit(1)
    return matches[0]["id"]


def get_version_summary(version_id: str) -> dict:
    """Fetch the what's-new summary for a single release version by id."""
    return api("GET", f"/api/admin/release-versions/{version_id}/summary")


def fmt_task(t: dict, verbose: bool = False) -> str:
    """Format a task as a one-line summary."""
    tid = t["id"][:8]
    status = t.get("status", "?")
    ttype = t.get("task_type", (t.get("payloads") or [{}])[0].get("type", "?"))
    title = t.get("title", "")[:50]
    msg = (t.get("latest_status_message") or "")[:80]
    parent = (t.get("parent_task_id") or "")[:8]
    ctx = t.get("context") or {}
    loop = ctx.get("review_loop", {})
    ci = loop.get("ci_waiting", False)
    pr = loop.get("pr_number", (t.get("payloads") or [{}])[0].get("pr_number", "-"))
    line = f"{tid}  [{ttype:8}] {status:13} {title:<50}"
    if verbose:
        line += f"  pr={pr}  ci={ci}  parent={parent or '-'}"
    if msg:
        line += f"  | {msg}"
    return line


# ── CLI entrypoint ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Blueprint orchestrator API helper")
    sub = parser.add_subparsers(dest="command")

    ct = sub.add_parser("create_task", help="Submit a new task")
    ct.add_argument("title", help="Short task title")
    ct.add_argument("prompt", help="Instructions for the agent")
    ct.add_argument("--type", dest="task_type", default="create",
                    choices=["create", "plan", "research", "review", "fix"],
                    help="Task type (default: create)")
    ct.add_argument("--base-branch", dest="base_branch", default=None)
    ct.add_argument("--pr-number", dest="pr_number", type=int, default=None)
    ct.add_argument("--branch", dest="branch", default=None)

    lt = sub.add_parser("list_tasks", help="Fetch recent tasks")
    lt.add_argument("--status", default=None, help="Filter by status")
    lt.add_argument("--limit", type=int, default=20, help="Max tasks to return")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "create_task":
            # fix and review types require --branch and --pr-number (they operate on existing PRs)
            if args.task_type in ("fix", "review") and not args.branch:
                print(f"Error: --branch is required for task type '{args.task_type}'", file=sys.stderr)
                sys.exit(1)
            if args.task_type in ("fix", "review") and not args.pr_number:
                print(f"Error: --pr-number is required for task type '{args.task_type}'", file=sys.stderr)
                sys.exit(1)
            result = create_task(
                title=args.title, prompt=args.prompt, task_type=args.task_type,
                base_branch=args.base_branch, pr_number=args.pr_number, branch=args.branch,
            )
            print(json.dumps(result, indent=2))
        elif args.command == "list_tasks":
            for t in list_tasks(limit=args.limit, status=args.status):
                print(fmt_task(t, verbose=True))
    except APIError:
        sys.exit(1)
