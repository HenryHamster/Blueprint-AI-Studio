"""Unified task management CLI.

Usage:
    python scripts/tasks.py list [--status STATUS] [--limit N] [-v]
    python scripts/tasks.py show <id_or_title>
    python scripts/tasks.py tree <parent_id>
    python scripts/tasks.py requeue <task_id> [<task_id> ...]
    python scripts/tasks.py cancel <task_id> [<task_id> ...]
    python scripts/tasks.py cancel-all [--status STATUS]
    python scripts/tasks.py evaluating
    python scripts/tasks.py requeue-ci
"""
import json
import sys
from blueprint_api import list_tasks, all_tasks_by_status, patch_task, fmt_task


def cmd_list(args):
    """List tasks, optionally filtered by status."""
    status = args[0] if args and not args[0].startswith("-") else None
    limit = 50
    verbose = False
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        if a == "-v" or a == "--verbose":
            verbose = True
        if a == "--status" and i + 1 < len(args):
            status = args[i + 1]

    statuses = [status] if status else ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED", "COMPLETED"]
    for st in statuses:
        tasks = list_tasks(status=st, limit=limit)
        if tasks:
            print(f"\n=== {st} ({len(tasks)}) ===")
            for t in tasks:
                print(f"  {fmt_task(t, verbose=verbose)}")


def cmd_show(args):
    """Show full details of a task by ID or title substring."""
    if not args:
        print("Usage: tasks.py show <id_or_title>")
        return
    query = args[0]
    for st in ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED", "COMPLETED", "CANCELLED"]:
        for t in list_tasks(status=st, limit=100):
            if t["id"].startswith(query) or query.lower() in t.get("title", "").lower():
                print(json.dumps({
                    "id": t["id"], "title": t["title"], "status": t["status"],
                    "task_type": t.get("task_type"),
                    "parent_task_id": t.get("parent_task_id"),
                    "vm_instance_id": t.get("vm_instance_id"),
                    "latest_status_message": (t.get("latest_status_message") or "")[:500],
                    "status_log": t.get("status_log", [])[-10:],
                    "payloads": t.get("payloads"),
                    "context": {k: v for k, v in (t.get("context") or {}).items() if not k.startswith("max_plan_")},
                    "error_logs": (t.get("error_logs") or "")[:500],
                    "attempt_count": t.get("attempt_count"),
                }, indent=2))
                return
    print(f"Task '{query}' not found.")


def cmd_tree(args):
    """Show a parent task and all its children."""
    if not args:
        print("Usage: tasks.py tree <parent_id>")
        return
    parent_id = args[0]
    all_t = all_tasks_by_status("QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED", "COMPLETED", "CANCELLED")

    parent = next((t for t in all_t if t["id"].startswith(parent_id)), None)
    if parent:
        full_id = parent["id"]
        print(f"Parent: {parent['title']}")
        print(f"Status: {parent['status']}")
        print(f"Message: {(parent.get('latest_status_message') or 'none')[:200]}")
        print()
        children = [t for t in all_t if (t.get("parent_task_id") or "").startswith(parent_id)]
        icons = {"QUEUED": "Q", "PROVISIONING": "P", "IN_PROGRESS": "R", "COMPLETED": "D",
                 "FAILED": "F", "CANCELLED": "X", "EVALUATING": "E"}
        print(f"Children: {len(children)}")
        for c in children:
            s = icons.get(c["status"], "?")
            pr = (c.get("payloads") or [{}])[0].get("pr_number", "-")
            merged = (c.get("context") or {}).get("pr_merged", False)
            title = c["title"][:60]
            print(f"  [{s}] {c['id'][:8]}  {title:<60}  pr={pr}  merged={merged}")
    else:
        print(f"Parent task '{parent_id}' not found.")


def cmd_requeue(args):
    """Requeue tasks by ID."""
    if not args:
        print("Usage: tasks.py requeue <task_id> [<task_id> ...]")
        return
    for tid in args:
        try:
            resp = patch_task(tid, {"status": "QUEUED"})
            print(f"  {tid[:8]}: {resp.get('status', 'ok')}")
        except Exception as e:
            print(f"  {tid[:8]}: FAILED — {e}")


def cmd_cancel(args):
    """Cancel tasks by ID."""
    if not args:
        print("Usage: tasks.py cancel <task_id> [<task_id> ...]")
        return
    for tid in args:
        try:
            resp = patch_task(tid, {"status": "CANCELLED"})
            print(f"  {tid[:8]}: cancelled")
        except Exception as e:
            print(f"  {tid[:8]}: FAILED — {e}")


def cmd_cancel_all(args):
    """Cancel all non-terminal tasks, optionally filtered by status."""
    statuses = [args[0]] if args else ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"]
    cancelled = 0
    for status in statuses:
        tasks = list_tasks(status=status, limit=100)
        for t in tasks:
            tid = t["id"]
            title = t["title"][:50]
            print(f"  Cancelling [{status[:4]}] {tid[:8]} {title}...", end=" ")
            try:
                patch_task(tid, {"status": "CANCELLED"})
                print("done")
                cancelled += 1
            except Exception as e:
                print(f"FAILED: {e}")
    print(f"\nCancelled {cancelled} tasks.")


def cmd_evaluating(args):
    """Show all EVALUATING tasks with CI-waiting info."""
    tasks = list_tasks(status="EVALUATING")
    if not tasks:
        print("No EVALUATING tasks.")
        return
    for t in tasks:
        ctx = t.get("context") or {}
        loop = ctx.get("review_loop", {})
        ci = loop.get("ci_waiting", False)
        pr = loop.get("pr_number", "-")
        print(f"  {t['id'][:8]}  {t['title'][:55]:<55}  ci_waiting={ci}  pr={pr}")


def cmd_requeue_ci(args):
    """Requeue all CI-waiting EVALUATING tasks."""
    tasks = list_tasks(status="EVALUATING")
    for t in tasks:
        ctx = t.get("context") or {}
        loop = ctx.get("review_loop", {})
        if not loop.get("ci_waiting"):
            continue
        tid = t["id"]
        pr = loop.get("pr_number", "?")
        print(f"  Requeuing {tid[:8]} (PR #{pr})...", end=" ")
        try:
            resp = patch_task(tid, {"status": "QUEUED"})
            print(f"-> {resp.get('status', 'ok')}")
        except Exception as e:
            print(f"FAILED: {e}")


COMMANDS = {
    "list": cmd_list,
    "show": cmd_show,
    "tree": cmd_tree,
    "requeue": cmd_requeue,
    "cancel": cmd_cancel,
    "cancel-all": cmd_cancel_all,
    "evaluating": cmd_evaluating,
    "requeue-ci": cmd_requeue_ci,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: tasks.py <command> [args...]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
