"""Read-only audit of Blueprint tasks and open GitHub PRs.

Usage:
    python scripts/audit_tasks_prs.py

The script combines deployed orchestrator task state with GitHub PR metadata.
It does not mutate tasks, branches, PRs, or repo state.
"""

from __future__ import annotations

import json
import subprocess
from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from blueprint_api import api, get_project_id, load_config


ACTIVE_STATUSES = {"QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"}
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
STATUSES = [
    "QUEUED",
    "PROVISIONING",
    "IN_PROGRESS",
    "EVALUATING",
    "FAILED",
    "COMPLETED",
    "CANCELLED",
]


def _safe(text: Any, limit: int = 96) -> str:
    value = "" if text is None else str(text)
    value = " ".join(value.split())
    return value[: limit - 1] + "..." if len(value) > limit else value


def _age(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
    except Exception:
        return iso[:19]
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _fetch_tasks(
    status: str | None,
    limit: int = 200,
    *,
    created_after: str | None = None,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if created_after:
            params["created_after"] = created_after
        path = f"/api/admin/tasks?{urlencode(params)}"
        data = api("GET", path)
        batch = data.get("tasks", data) if isinstance(data, dict) else data
        if not batch:
            break
        tasks.extend(batch)
        if max_rows and len(tasks) >= max_rows:
            return tasks[:max_rows]
        if len(batch) < limit:
            break
        offset += limit
    return tasks


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _task_type(task: dict[str, Any]) -> str:
    return task.get("task_type") or ((task.get("payloads") or [{}])[0].get("type")) or "?"


def _task_pr_numbers(task: dict[str, Any]) -> set[int]:
    numbers: set[int] = set()
    payload = (task.get("payloads") or [{}])[0] or {}
    ctx = task.get("context") or {}
    loop = ctx.get("review_loop") or {}
    candidates = [
        payload.get("pr_number"),
        loop.get("pr_number"),
        ctx.get("rollup_pr_number"),
    ]

    artifacts = task.get("artifacts") or []
    if isinstance(artifacts, dict):
        artifacts = [artifacts]
    for art in artifacts:
        if not isinstance(art, dict):
            continue
        pr_url = art.get("pr_url") or art.get("url")
        if pr_url:
            candidates.append(str(pr_url).rstrip("/").split("/")[-1])

    for raw in candidates:
        try:
            if raw is not None:
                numbers.add(int(raw))
        except (TypeError, ValueError):
            continue
    return numbers


def _task_pr_url(task: dict[str, Any]) -> str:
    ctx = task.get("context") or {}
    if task.get("pr_url"):
        return str(task["pr_url"])
    if ctx.get("rollup_pr_url"):
        return str(ctx["rollup_pr_url"])
    for art in task.get("artifacts") or []:
        if isinstance(art, dict) and art.get("pr_url"):
            return str(art["pr_url"])
    nums = sorted(_task_pr_numbers(task))
    return f"PR #{nums[0]}" if nums else "(no PR link)"


def _changed_files(task: dict[str, Any]) -> list[str]:
    ctx = task.get("context") or {}
    raw = ctx.get("_pr_changed_files") or task.get("changed_files") or []
    if not raw and isinstance(ctx.get("_file_change_details"), list):
        raw = [item.get("path") for item in ctx["_file_change_details"] if isinstance(item, dict)]
    if not raw and isinstance(task.get("file_change_details"), list):
        raw = [item.get("path") for item in task["file_change_details"] if isinstance(item, dict)]
    return [str(path) for path in raw if path]


def _task_line(task: dict[str, Any], width: int = 86) -> str:
    task_id = str(task.get("id", ""))[:8]
    status = task.get("status", "?")
    ttype = _task_type(task)
    title = _safe(task.get("title", "?"), width)
    msg = _safe(task.get("latest_status_message") or "", 96)
    suffix = f" | {msg}" if msg else ""
    return f"{task_id} [{ttype:8}] {status:13} {title}{suffix}"


def _check_summary(pr: dict[str, Any]) -> tuple[str, int, int, int]:
    checks = pr.get("statusCheckRollup") or []
    total = len(checks)
    failing = 0
    pending = 0
    successful = 0
    for check in checks:
        status = check.get("status")
        conclusion = check.get("conclusion")
        if status != "COMPLETED":
            pending += 1
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            successful += 1
        else:
            failing += 1
    if pending:
        state = "pending"
    elif failing:
        state = "failing"
    elif total:
        state = "passing"
    else:
        state = "none"
    return state, successful, failing, pending


def _gh_open_prs(repo: str) -> list[dict[str, Any]]:
    fields = [
        "number",
        "title",
        "url",
        "headRefName",
        "baseRefName",
        "isDraft",
        "mergeable",
        "mergeStateStatus",
        "reviewDecision",
        "statusCheckRollup",
        "createdAt",
        "updatedAt",
        "author",
        "labels",
    ]
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        ",".join(fields),
    ]
    completed = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return json.loads(completed.stdout)


def _classify_pr(pr: dict[str, Any], linked_tasks: list[dict[str, Any]]) -> str:
    check_state, _ok, failing, pending = _check_summary(pr)
    active = [t for t in linked_tasks if t.get("status") in ACTIVE_STATUSES]
    failed = [t for t in linked_tasks if t.get("status") == "FAILED"]

    if pr.get("isDraft"):
        return "leave alone: draft PR"
    if pending:
        return "wait for CI"
    if failing:
        return "fix CI before review/merge"
    if pr.get("mergeStateStatus") in {"DIRTY", "UNKNOWN"} or pr.get("mergeable") == "CONFLICTING":
        return "needs conflict resolution"
    if active:
        active_types = sorted({_task_type(t) for t in active})
        if "review" in active_types:
            return "run queued review, then merge if approved"
        if "fix" in active_types:
            return "run queued fix, then re-review"
        if "plan" in active_types or "research" in active_types:
            return "let active parent/rollup finish"
        return "let active task finish"
    if failed:
        return "inspect failed linked task before merging"
    if check_state == "passing" and pr.get("mergeable") == "MERGEABLE":
        return "candidate for human review/merge"
    return "inspect manually"


def _args(argv: list[str] | None = None):
    parser = ArgumentParser(description="Read-only audit of Blueprint tasks and PRs.")
    parser.add_argument("--created-after", help="ISO timestamp lower bound for task.created_at")
    parser.add_argument("--created-before", help="ISO timestamp upper bound for task.created_at")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    cfg = load_config()
    repo = cfg.get("default_repo")
    project_id = get_project_id(cfg.get("project_id"))

    print(f"Project: {project_id}")
    print(f"Repo:    {repo}")
    print()

    deploy = api("GET", "/api/admin/deploy-state")
    settings = api("GET", "/api/admin/settings")
    diagnostics = api("GET", "/api/admin/diagnostics")
    digest = api("GET", f"/api/admin/digest?hours=72&project_id={project_id}")
    open_prs_widget = api("GET", f"/api/admin/open-prs?project_id={project_id}").get("open_prs", [])

    tasks_by_status = {status: _fetch_tasks(status) for status in STATUSES}
    tasks = [task for batch in tasks_by_status.values() for task in batch]
    tasks_by_id = {task["id"]: task for task in tasks if task.get("id")}
    pr_to_tasks: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        for pr_num in _task_pr_numbers(task):
            pr_to_tasks[pr_num].append(task)

    try:
        gh_prs = _gh_open_prs(repo)
        gh_error = None
    except Exception as exc:
        gh_prs = []
        gh_error = str(exc)

    print("=== System ===")
    print(f"Deploy status:       {deploy.get('status')} ({deploy.get('pause_reason') or 'no pause reason'})")
    print(f"Current revision:    {deploy.get('current_revision')}")
    print(f"Max VMs/deploy:      {deploy.get('max_concurrent_vms')}")
    print(f"Max VMs/settings:    {settings.get('max_concurrent_vms')}")
    print(f"Failure rate:        {deploy.get('failure_rate')}")
    print(f"Diagnostics stuck:   {diagnostics.get('total_stuck_count')}")
    print(f"Circuit breakers:    {diagnostics.get('circuit_breakers') or {}}")
    print()

    print("=== Task Counts ===")
    for status in ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED", "COMPLETED", "CANCELLED"]:
        print(f"{status:13} {len(tasks_by_status.get(status, []))}")
    active_tasks = [t for t in tasks if t.get("status") in ACTIVE_STATUSES]
    active_counts = Counter(_task_type(t) for t in active_tasks)
    print(f"Active by type:      {dict(sorted(active_counts.items()))}")
    print()

    print("=== Needs Attention From Digest (72h) ===")
    for item in digest.get("needs_attention", [])[:30]:
        print(
            f"{str(item.get('task_id', '?'))[:8]} "
            f"[{item.get('reason', '?')}] "
            f"{_safe(item.get('title', '?'), 78)} "
            f"({item.get('waiting_hours', '?')}h)"
        )
        if item.get("error_snippet"):
            print(f"  {_safe(item.get('error_snippet'), 120)}")
    if not digest.get("needs_attention"):
        print("(none)")
    print()

    print("=== Active / Failed Tasks ===")
    for status in ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED"]:
        batch = tasks_by_status.get(status, [])
        if not batch:
            continue
        print(f"-- {status} ({len(batch)})")
        for task in batch:
            print(_task_line(task))
        print()

    print("=== Open PRs ===")
    if gh_error:
        print(f"GitHub CLI unavailable: {gh_error}")
        print(f"Dashboard open PRs: {len(open_prs_widget)}")
        for pr in sorted(open_prs_widget, key=lambda p: p.get("pr_number") or 0, reverse=True):
            linked = pr.get("tasks") or []
            task_desc = ", ".join(
                f"{str(t.get('task_id', ''))[:8]}:{t.get('task_type')}:{t.get('task_status')}"
                for t in linked
            ) or "orphan"
            print(f"#{pr.get('pr_number')} {pr.get('pr_branch')} {task_desc} {_safe(pr.get('pr_title'), 80)}")
        return 0

    print(f"GitHub open PRs:     {len(gh_prs)}")
    print(f"Dashboard open PRs:  {len(open_prs_widget)}")
    print()

    for pr in sorted(gh_prs, key=lambda p: p.get("number") or 0, reverse=True):
        number = pr["number"]
        linked = sorted(
            pr_to_tasks.get(number, []),
            key=lambda t: (t.get("status") not in ACTIVE_STATUSES, t.get("created_at") or ""),
        )
        check_state, ok, failing, pending = _check_summary(pr)
        action = _classify_pr(pr, linked)
        task_desc = ", ".join(
            f"{str(t.get('id', ''))[:8]}:{_task_type(t)}:{t.get('status')}"
            for t in linked[:6]
        )
        if len(linked) > 6:
            task_desc += f", +{len(linked) - 6} more"
        task_desc = task_desc or "no linked task in fetched set"
        print(
            f"#{number:<5} {pr.get('mergeStateStatus','?'):<8} {check_state:<7} "
            f"checks={ok}/{failing}/{pending} review={pr.get('reviewDecision') or '-':<16} "
            f"age={_age(pr.get('createdAt')):<4} base={pr.get('baseRefName')} "
            f"head={pr.get('headRefName')}"
        )
        print(f"       action: {action}")
        print(f"       tasks:  {task_desc}")
        print(f"       title:  {_safe(pr.get('title'), 120)}")
    print()

    print("=== Suggested Buckets ===")
    buckets: dict[str, list[int]] = defaultdict(list)
    for pr in gh_prs:
        buckets[_classify_pr(pr, pr_to_tasks.get(pr["number"], []))].append(pr["number"])
    for action, numbers in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        formatted = ", ".join(f"#{n}" for n in sorted(numbers))
        print(f"{action}: {formatted}")

    stuck_ids = {str(s.get("task_id")) for s in diagnostics.get("stuck_tasks", []) if s.get("task_id")}
    if stuck_ids:
        print()
        print("=== Stuck Task IDs ===")
        for tid in sorted(stuck_ids):
            task = tasks_by_id.get(tid)
            print(_task_line(task) if task else tid[:8])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
