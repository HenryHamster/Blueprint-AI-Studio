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
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

CONFIG_FILE = ".blueprint-config.json"

_config_cache: dict | None = None


def load_config() -> dict:
    """Load API config from project root. Exits with guidance if missing."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    config_path = Path(__file__).resolve().parent.parent / CONFIG_FILE
    if not config_path.exists():
        print(f"ERROR: {config_path} not found.")
        print("Create it with: api_url, api_key, default_repo, default_base_branch")
        sys.exit(1)
    _config_cache = json.loads(config_path.read_text())
    return _config_cache


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
        print(f"API error {e.code}: {e.read().decode()[:500]}")
        raise


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
    """Return a project ID. Uses explicit arg, config, or fetches the first project."""
    global _project_id_cache
    if project_id:
        return project_id
    cfg = load_config()
    if cfg.get("project_id"):
        return cfg["project_id"]
    if _project_id_cache:
        return _project_id_cache
    resp = api("GET", "/api/admin/projects")
    projects = resp.get("items", []) if isinstance(resp, dict) else resp
    if not projects:
        print("ERROR: No projects found.")
        sys.exit(1)
    _project_id_cache = projects[0]["id"]
    return _project_id_cache


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
