"""Blueprint orchestrator API helper.

Used by Claude Code /task and /tasks commands to interact with the deployed orchestrator.
Reads auth and defaults from .blueprint-config.json — never hardcodes secrets.
"""

import json
import sys
import urllib.request
from pathlib import Path

CONFIG_FILE = ".blueprint-config.json"


def _load_config() -> dict:
    """Load API config from project root. Exits with guidance if missing."""
    config_path = Path(__file__).resolve().parent.parent / CONFIG_FILE
    if not config_path.exists():
        print(f"ERROR: {config_path} not found.")
        print("Create it with: api_url, api_key, default_repo, default_base_branch")
        sys.exit(1)
    return json.loads(config_path.read_text())


def _api_request(method: str, path: str, body: dict | None = None) -> dict:
    """Make an authenticated request to the orchestrator API."""
    config = _load_config()
    url = config["api_url"].rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"API error {e.code}: {e.read().decode()}")
        sys.exit(1)


# ── Public API ───────────────────────────────────────────────────────────────

def create_task(
    title: str,
    prompt: str,
    task_type: str = "create",
    base_branch: str | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
) -> dict:
    """Submit a new task to the orchestrator.

    Args:
        title:       Short task title (conventional commit style).
        prompt:      Instructions for the agent.
        task_type:   One of: create, plan, research, review, fix.
        base_branch: Target branch (defaults to config's default_base_branch).
        pr_number:   Required for review/fix tasks.
        branch:      PR head branch — required for fix, optional for review.
    """
    config = _load_config()
    payload = {
        "github_repo": config["default_repo"],
        "title": title,
        "prompt": prompt,
        "base_branch": base_branch or config.get("default_base_branch", "main"),
        "task_type": task_type,
    }
    if pr_number is not None:
        payload["pr_number"] = pr_number
    if branch is not None:
        payload["branch"] = branch
    return _api_request("POST", "/api/admin/tasks", payload)


def list_tasks(limit: int = 10, status: str | None = None) -> dict:
    """Fetch recent tasks, optionally filtered by status."""
    path = f"/api/admin/tasks?limit={limit}"
    if status:
        path += f"&status={status}"
    return _api_request("GET", path)


# ── CLI entrypoint ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python blueprint_api.py <command> [args...]")
        print("  create_task <title> <prompt> [task_type]")
        print("  list_tasks  [status_filter]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "create_task":
        result = create_task(
            title=sys.argv[2] if len(sys.argv) > 2 else "Untitled task",
            prompt=sys.argv[3] if len(sys.argv) > 3 else "",
            task_type=sys.argv[4] if len(sys.argv) > 4 else "create",
        )
    elif cmd == "list_tasks":
        result = list_tasks(status=sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

    print(json.dumps(result, indent=2))
