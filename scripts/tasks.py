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
    python scripts/tasks.py trigger-rollup [<parent_id>]    — mark unmerged children as merged, trigger rollup
    python scripts/tasks.py children <parent_id> [--all]   — list children with merge status
"""
import json
import sys
from blueprint_api import list_tasks, all_tasks_by_status, patch_task, fmt_task


def _resolve_task(query):
    """Find a task by ID prefix or title substring. Returns full task dict or None."""
    for st in ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED", "COMPLETED", "CANCELLED"]:
        for t in list_tasks(status=st, limit=200):
            if t["id"].startswith(query) or query.lower() in t.get("title", "").lower():
                return t
    return None


def _get_children(parent_id):
    """Get all children of a parent task across all statuses."""
    all_t = all_tasks_by_status("QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED", "COMPLETED", "CANCELLED")
    return [t for t in all_t if t.get("parent_task_id") and t["parent_task_id"].startswith(parent_id)]


def _safe(s, maxlen=60):
    """Truncate and ASCII-encode for safe terminal output."""
    return s[:maxlen].encode("ascii", "replace").decode()


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
    t = _resolve_task(args[0])
    if not t:
        print(f"Task '{args[0]}' not found.")
        return
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
    }, indent=2, ensure_ascii=True))


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
        print(f"Parent: {_safe(parent['title'], 80)}")
        print(f"Status: {parent['status']}")
        print(f"Message: {_safe(parent.get('latest_status_message') or 'none', 200)}")
        print()
        children = [t for t in all_t if (t.get("parent_task_id") or "").startswith(parent_id)]
        icons = {"QUEUED": "Q", "PROVISIONING": "P", "IN_PROGRESS": "R", "COMPLETED": "D",
                 "FAILED": "F", "CANCELLED": "X", "EVALUATING": "E"}
        print(f"Children: {len(children)}")
        for c in children:
            s = icons.get(c["status"], "?")
            ptype = (c.get("payloads") or [{}])[0].get("type", "?")
            pr = (c.get("payloads") or [{}])[0].get("pr_number", "-")
            merged = (c.get("context") or {}).get("pr_merged", False)
            title = _safe(c["title"])
            print(f"  [{s}] {c['id'][:8]}  [{ptype:8}]  {title:<55}  pr={pr}  merged={merged}")
    else:
        print(f"Parent task '{parent_id}' not found.")


def cmd_children(args):
    """List children of a parent, filtering out review/fix unless --all."""
    if not args:
        print("Usage: tasks.py children <parent_id> [--all]")
        return
    parent_id = args[0]
    show_all = "--all" in args

    children = _get_children(parent_id)
    if not children:
        print(f"No children found for {parent_id[:8]}.")
        return

    icons = {"QUEUED": "Q", "PROVISIONING": "P", "IN_PROGRESS": "R", "COMPLETED": "D",
             "FAILED": "F", "CANCELLED": "X", "EVALUATING": "E"}
    for c in children:
        ptype = (c.get("payloads") or [{}])[0].get("type", "?")
        if not show_all and ptype in ("review", "fix", "debug"):
            continue
        s = icons.get(c["status"], "?")
        merged = (c.get("context") or {}).get("pr_merged", False)
        pr = (c.get("payloads") or [{}])[0].get("pr_number", "-")
        title = _safe(c["title"])
        print(f"  [{s}] {c['id'][:8]}  [{ptype:8}] {c['status']:13}  merged={str(merged):5}  pr={pr:<4}  {title}")


def cmd_requeue(args):
    """Requeue tasks by ID."""
    if not args:
        print("Usage: tasks.py requeue <task_id> [<task_id> ...]")
        return
    for tid in args:
        t = _resolve_task(tid)
        if not t:
            print(f"  {tid[:8]}: not found")
            continue
        try:
            resp = patch_task(t["id"], {"status": "QUEUED"})
            print(f"  {tid[:8]}: {resp.get('status', 'ok')}")
        except Exception as e:
            print(f"  {tid[:8]}: FAILED -- {e}")


def cmd_cancel(args):
    """Cancel tasks by ID."""
    if not args:
        print("Usage: tasks.py cancel <task_id> [<task_id> ...]")
        return
    for tid in args:
        t = _resolve_task(tid)
        if not t:
            print(f"  {tid[:8]}: not found")
            continue
        try:
            resp = patch_task(t["id"], {"status": "CANCELLED"})
            print(f"  {tid[:8]}: cancelled")
        except Exception as e:
            print(f"  {tid[:8]}: FAILED -- {e}")


def cmd_cancel_all(args):
    """Cancel all non-terminal tasks, optionally filtered by status."""
    statuses = [args[0]] if args else ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"]
    cancelled = 0
    for status in statuses:
        tasks = list_tasks(status=status, limit=100)
        for t in tasks:
            tid = t["id"]
            title = _safe(t["title"], 50)
            print(f"  Cancelling [{status[:4]}] {tid[:8]} {title}...", end=" ")
            try:
                patch_task(tid, {"status": "CANCELLED"})
                print("done")
                cancelled += 1
            except Exception as e:
                print(f"FAILED: {e}")
    print(f"\nCancelled {cancelled} tasks.")


def cmd_evaluating(args):
    """Show all EVALUATING tasks with blocking info."""
    tasks = list_tasks(status="EVALUATING")
    if not tasks:
        print("No EVALUATING tasks.")
        return
    for t in tasks:
        ctx = t.get("context") or {}
        loop = ctx.get("review_loop", {})
        ci = loop.get("ci_waiting", False)
        pr = loop.get("pr_number", "-")
        msg = _safe(t.get("latest_status_message") or "", 80)
        print(f"  {t['id'][:8]}  {_safe(t['title'], 55):<55}  ci={ci}  pr={pr}")
        if msg:
            print(f"           {msg}")


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


def cmd_trigger_rollup(args):
    """Mark unmerged COMPLETED children as merged to trigger rollup on stuck parents.

    Finds EVALUATING research/plan parents with COMPLETED children missing
    pr_merged, patches them to set pr_merged=true, which triggers the backend
    rollup check. Run without args to scan all, or pass a parent ID.
    """
    if args:
        parents = [_resolve_task(args[0])]
        if not parents[0]:
            print(f"Task '{args[0]}' not found.")
            return
    else:
        parents = list_tasks(status="EVALUATING", limit=200)

    parents = [p for p in parents if p and p["status"] == "EVALUATING"
               and (p.get("payloads") or [{}])[0].get("type") in ("research", "plan")]

    if not parents:
        print("No stuck EVALUATING research/plan parents found.")
        return

    all_tasks = all_tasks_by_status("COMPLETED", "FAILED", "CANCELLED", "QUEUED",
                                    "IN_PROGRESS", "PROVISIONING", "EVALUATING",
                                    limit=500)

    for parent in parents:
        pid = parent["id"]
        children = [t for t in all_tasks if t.get("parent_task_id") == pid]
        work_children = [c for c in children
                         if (c.get("payloads") or [{}])[0].get("type") not in ("review", "fix", "debug")]

        unmerged = [c for c in work_children
                    if c["status"] == "COMPLETED"
                    and not (c.get("context") or {}).get("pr_merged")]

        failed = [c for c in work_children if c["status"] == "FAILED"]
        active = [c for c in work_children
                  if c["status"] in ("QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING")]

        print(f"\n  {pid[:8]}  {_safe(parent['title'], 60)}")
        total = len(work_children)
        merged = len([c for c in work_children if (c.get("context") or {}).get("pr_merged")])
        cancelled = len([c for c in work_children if c["status"] == "CANCELLED"])
        print(f"  Children: {total} total, {merged} merged, {len(unmerged)} unmerged, "
              f"{len(failed)} failed, {len(active)} active, {cancelled} cancelled")

        if active:
            print(f"  Skipping -- {len(active)} children still active")
            continue

        if not unmerged:
            if failed:
                print(f"  No unmerged children, but {len(failed)} failed. Use: tasks.py requeue {pid[:8]}")
                continue
            # All merged but parent still EVALUATING -- force rollup by re-completing a child
            any_merged = next((c for c in work_children
                               if c["status"] == "COMPLETED" and (c.get("context") or {}).get("pr_merged")), None)
            if any_merged:
                print(f"  All children merged but rollup didn't fire. Re-triggering...")
                try:
                    patch_task(any_merged["id"], {"status": "COMPLETED"})
                    print(f"    Re-completed {any_merged['id'][:8]} to trigger rollup")
                except Exception as e:
                    print(f"    FAILED: {e}")
            else:
                print(f"  Nothing to do -- all children cancelled")
            continue

        for c in unmerged:
            title = _safe(c["title"], 50)
            print(f"    Patching {c['id'][:8]}  {title}")
            try:
                patch_task(c["id"], {"context": {"pr_merged": True}})
            except Exception as e:
                print(f"    FAILED: {e}")

        print(f"  Patched {len(unmerged)} children. Rollup should trigger.")


COMMANDS = {
    "list": cmd_list,
    "show": cmd_show,
    "tree": cmd_tree,
    "children": cmd_children,
    "requeue": cmd_requeue,
    "cancel": cmd_cancel,
    "cancel-all": cmd_cancel_all,
    "evaluating": cmd_evaluating,
    "requeue-ci": cmd_requeue_ci,
    "trigger-rollup": cmd_trigger_rollup,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: tasks.py <command> [args...]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
