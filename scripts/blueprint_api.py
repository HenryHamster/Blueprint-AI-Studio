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
    import argparse

    parser = argparse.ArgumentParser(description="Blueprint orchestrator API helper")
    sub = parser.add_subparsers(dest="command")

    ct = sub.add_parser("create_task", help="Submit a new task")
    ct.add_argument("title", help="Short task title")
    ct.add_argument("prompt", help="Instructions for the agent")
    ct.add_argument("--type", dest="task_type", default="create",
                    choices=["create", "plan", "research", "review", "fix"],
                    help="Task type (default: create)")
    ct.add_argument("--base-branch", dest="base_branch", default=None,
                    help="Target branch (default: config's default_base_branch)")
    ct.add_argument("--pr-number", dest="pr_number", type=int, default=None,
                    help="PR number (required for review/fix)")
    ct.add_argument("--branch", dest="branch", default=None,
                    help="PR head branch (required for fix, optional for review)")

    lt = sub.add_parser("list_tasks", help="Fetch recent tasks")
    lt.add_argument("--status", default=None, help="Filter by status")
    lt.add_argument("--limit", type=int, default=10, help="Max tasks to return")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "create_task":
        result = create_task(
            title=args.title,
            prompt=args.prompt,
            task_type=args.task_type,
            base_branch=args.base_branch,
            pr_number=args.pr_number,
            branch=args.branch,
        )
    elif args.command == "list_tasks":
        result = list_tasks(limit=args.limit, status=args.status)

    print(json.dumps(result, indent=2))
