"""Unified task management CLI.

Usage:
    python scripts/tasks.py list [--status STATUS] [--limit N] [-v]
    python scripts/tasks.py show <id_or_title>
    python scripts/tasks.py scope <id_or_title> [-v]       — show prompt, research question, payload
    python scripts/tasks.py payload <id_or_title>          — dump full raw payload + context JSON
    python scripts/tasks.py tree <parent_id>
    python scripts/tasks.py children <parent_id> [--all]   — list children with merge status
    python scripts/tasks.py diagnose                       — health check all active tasks
    python scripts/tasks.py failures                       — analyze failed tasks, group by root cause
    python scripts/tasks.py health                         — system health: autonomy, deploy, settings
    python scripts/tasks.py evaluating [-v]                — EVALUATING tasks (verbose: research Q + children)
    python scripts/tasks.py autonomy-debug [project_id]    — why autonomy isn't spawning
    python scripts/tasks.py complete <task_id> [--message]
    python scripts/tasks.py set-status <task_id> <status> [--message]
    python scripts/tasks.py requeue <task_id> [...]
    python scripts/tasks.py cancel <task_id> [...]
    python scripts/tasks.py cancel-all [--status STATUS]
    python scripts/tasks.py requeue-ci
    python scripts/tasks.py trigger-rollup [<parent_id>]
    python scripts/tasks.py archive [--completed]          — archive old terminal tasks
    python scripts/tasks.py archive --pipelines            — archive fully-terminal pipelines (parent+all children done)
    python scripts/tasks.py research [--limit N] [-v]     — recent research results + findings
    python scripts/tasks.py research <id> [-v]            — detailed view of one research task
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
    """Show full details of a task by ID or title substring.

    Flags:
      --transitions   Show only status transitions from the full log (not heartbeats)
      --heartbeats    Show ALL log entries including heartbeats (with optional --since filter)
      --full-log      Show the entire status_log as JSON (can be very long)
      --since TIME    Filter entries after this ISO timestamp (e.g. --since 2026-04-03T08:30)
    """
    if not args:
        print("Usage: tasks.py show <id_or_title> [--transitions] [--heartbeats] [--full-log]")
        return
    show_transitions = "--transitions" in args
    show_heartbeats = "--heartbeats" in args
    show_full_log = "--full-log" in args
    query = [a for a in args if not a.startswith("--")][0]

    t = _resolve_task(query)
    if not t:
        print(f"Task '{query}' not found.")
        return

    # For --transitions/--heartbeats, fetch full task from admin API to get complete status_log
    if show_transitions or show_heartbeats or show_full_log:
        from blueprint_api import api
        try:
            full = api("GET", f"/api/admin/tasks/{t['id']}")
            t = full.get("task", full)
        except Exception:
            pass  # Fall back to list_tasks data

    if show_transitions or show_heartbeats:
        log = t.get("status_log", [])
        since = None
        for i, a in enumerate(args):
            if a == "--since" and i + 1 < len(args):
                since = args[i + 1]
        print(f"Task: {_safe(t.get('title', '?'), 80)}")
        print(f"Total log entries: {len(log)}")
        print(f"Created: {t.get('created_at', '?')}")
        print(f"Attempts: {t.get('attempt_count', '?')}")
        print(f"Error: {_safe((t.get('error_logs') or '')[:300], 300)}")
        print()
        label = "All Log Entries" if show_heartbeats else "Status Transitions"
        print(f"=== {label} ===")
        for e in log:
            ts = e.get("t", "")
            if since and ts < since:
                continue
            msg = e.get("msg", "")
            if show_heartbeats or any(kw in msg for kw in ["Status changed", "Failed", "Reaped", "Fatal"]):
                print(f"  {ts}  {_safe(msg, 150)}")
        return

    log_entries = t.get("status_log", [])
    if not show_full_log:
        log_entries = log_entries[-10:]

    print(json.dumps({
        "id": t["id"], "title": t["title"], "status": t["status"],
        "task_type": t.get("task_type"),
        "parent_task_id": t.get("parent_task_id"),
        "vm_instance_id": t.get("vm_instance_id"),
        "latest_status_message": (t.get("latest_status_message") or "")[:500],
        "status_log": log_entries,
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
        # Build lookup for dep resolution
        id_map = {t["id"]: t for t in all_t}
        print(f"Children: {len(children)}")
        for c in children:
            s = icons.get(c["status"], "?")
            ptype = (c.get("payloads") or [{}])[0].get("type", "?")
            pr = (c.get("payloads") or [{}])[0].get("pr_number", "-")
            merged = (c.get("context") or {}).get("pr_merged", False)
            title = _safe(c["title"])
            deps = c.get("depends_on") or []
            dep_str = ""
            if deps:
                parts = []
                for d in deps:
                    dt = id_map.get(d)
                    if dt:
                        parts.append(f"{d[:8]}={dt['status'][:4]}")
                    else:
                        parts.append(f"{d[:8]}=???")
                dep_str = f"  deps=[{', '.join(parts)}]"
            print(f"  [{s}] {c['id'][:8]}  [{ptype:8}]  {title:<55}  pr={pr}  merged={merged}{dep_str}")
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
    """Cancel all non-terminal tasks, optionally filtered by status or project.

    Usage: cancel-all [--status STATUS] [--project PROJECT_ID_OR_NAME]
    """
    from blueprint_api import api
    status_filter = None
    project_filter = None
    i = 0
    positional_args = list(args)
    while i < len(positional_args):
        if positional_args[i] == "--status" and i + 1 < len(positional_args):
            status_filter = positional_args[i + 1]
            i += 2
        elif positional_args[i] == "--project" and i + 1 < len(positional_args):
            project_filter = positional_args[i + 1]
            i += 2
        else:
            # Legacy: first positional arg is status
            if not status_filter:
                status_filter = positional_args[i]
            i += 1

    # Resolve project filter to ID
    project_id = None
    if project_filter:
        projects = api("GET", "/api/admin/projects")
        for p in projects:
            if p["id"].startswith(project_filter) or project_filter.lower() in p.get("name", "").lower():
                project_id = p["id"]
                print(f"Filtering to project: {p.get('name', p['id'][:8])}")
                break
        if not project_id:
            print(f"ERROR: No project matching '{project_filter}'")
            return

    statuses = [status_filter] if status_filter else ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"]
    cancelled = 0
    for status in statuses:
        tasks = list_tasks(status=status, limit=200)
        for t in tasks:
            if project_id and t.get("project_id") != project_id:
                continue
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
    """Show all EVALUATING tasks with scope, research question, and blocking info."""
    from blueprint_api import api
    tasks = list_tasks(status="EVALUATING")
    if not tasks:
        print("No EVALUATING tasks.")
        return
    verbose = "-v" in args or "--verbose" in args
    for t in tasks:
        ctx = t.get("context") or {}
        loop = ctx.get("review_loop", {})
        ci = loop.get("ci_waiting", False)
        pr = loop.get("pr_number", "-")
        ptype = (t.get("payloads") or [{}])[0].get("type", t.get("task_type", "?"))
        msg = _safe(t.get("latest_status_message") or "", 80)
        print(f"  {t['id'][:8]}  [{ptype:8}]  {_safe(t['title'], 50):<50}  ci={ci}  pr={pr}")
        if msg:
            print(f"           {msg}")
        # Show scope for research/plan parents
        if verbose and ptype in ("research", "plan"):
            try:
                full = api("GET", f"/api/admin/tasks/{t['id']}")
                ft = full.get("task", full)
                payloads = ft.get("payloads") or []
                if payloads:
                    prompt = payloads[0].get("prompt", "")
                    scope = (ft.get("context") or {}).get("scope", "")
                    rq = (ft.get("context") or {}).get("research_question", "")
                    if rq:
                        print(f"           Q: {_safe(rq, 120)}")
                    if scope:
                        print(f"           Scope: {_safe(scope, 100)}")
                    if prompt and not rq:
                        print(f"           Prompt: {_safe(prompt, 120)}")
                # Show child summary
                children = _get_children(t['id'][:8])
                if children:
                    by_status = {}
                    for c in children:
                        by_status.setdefault(c["status"], []).append(c)
                    parts = [f"{s}={len(v)}" for s, v in sorted(by_status.items())]
                    merged = sum(1 for c in children if (c.get("context") or {}).get("pr_merged"))
                    print(f"           Children: {len(children)} ({', '.join(parts)}, merged={merged})")
            except Exception:
                pass


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


def cmd_diagnose(args):
    """Health check all active tasks — shows attempts, errors, memory usage."""
    statuses = ["QUEUED", "PROVISIONING", "IN_PROGRESS"]
    all_active = []
    for st in statuses:
        all_active.extend(list_tasks(status=st, limit=100))

    if not all_active:
        print("No active tasks.")
        return

    # Sort by attempt_count descending (most troubled first)
    all_active.sort(key=lambda t: t.get("attempt_count", 0), reverse=True)

    max_retries = 3
    icons = {"QUEUED": "Q", "PROVISIONING": "P", "IN_PROGRESS": "R"}
    warn_count = 0

    print(f"\n=== ACTIVE TASKS ({len(all_active)}) ===")
    for t in all_active:
        tid = t["id"][:8]
        status = t["status"]
        icon = icons.get(status, "?")
        attempts = t.get("attempt_count", 0)
        ttype = (t.get("payloads") or [{}])[0].get("type", t.get("task_type", "?"))
        title = _safe(t.get("title", ""), 50)
        msg = t.get("latest_status_message") or ""
        error = (t.get("error_logs") or "")[:200]

        # Extract memory from heartbeat message [XXmb]
        mem = ""
        if msg.startswith("["):
            bracket_end = msg.find("]")
            if bracket_end > 0:
                mem = msg[1:bracket_end]

        # Warning indicators
        warn = ""
        if attempts >= max_retries:
            warn = " *** FINAL ATTEMPT"
            warn_count += 1
        elif attempts >= 2:
            warn = " ** high retries"
            warn_count += 1

        print(f"  [{icon}] {tid}  [{ttype:8}]  attempt {attempts}/{max_retries}  {f'[{mem}]':>10}  {title}{warn}")
        if error:
            # Show first line of error
            first_line = error.split("\n")[0][:120]
            print(f"      ERROR: {first_line}")

    # Show EVALUATING parents summary
    parents = list_tasks(status="EVALUATING", limit=50)
    if parents:
        print(f"\n=== EVALUATING PARENTS ({len(parents)}) ===")
        for p in parents:
            pid = p["id"][:8]
            title = _safe(p.get("title", ""), 55)
            msg = _safe(p.get("latest_status_message") or "none", 80)
            print(f"  {pid}  {title}")
            print(f"           {msg}")

    if warn_count:
        print(f"\n  WARNING: {warn_count} task(s) at high retry count")


def cmd_complete(args):
    """Mark a task as COMPLETED with optional message."""
    if not args:
        print("Usage: tasks.py complete <task_id> [--message MSG]")
        return
    tid = args[0]
    message = None
    for i, a in enumerate(args):
        if a in ("--message", "-m") and i + 1 < len(args):
            message = args[i + 1]

    t = _resolve_task(tid)
    if not t:
        print(f"Task '{tid}' not found.")
        return

    payload = {"status": "COMPLETED"}
    if message:
        payload["latest_status_message"] = message
    try:
        resp = patch_task(t["id"], payload)
        print(f"  {t['id'][:8]}: {resp.get('status', 'ok')} — marked COMPLETED")
    except Exception as e:
        print(f"  {t['id'][:8]}: FAILED -- {e}")


def cmd_set_status(args):
    """Set arbitrary status on a task with optional message."""
    if len(args) < 2:
        print("Usage: tasks.py set-status <task_id> <STATUS> [--message MSG]")
        print("  STATUS: QUEUED, PROVISIONING, IN_PROGRESS, EVALUATING, COMPLETED, FAILED, CANCELLED")
        return
    tid = args[0]
    new_status = args[1].upper()
    valid = {"QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "COMPLETED", "FAILED", "CANCELLED"}
    if new_status not in valid:
        print(f"Invalid status '{new_status}'. Must be one of: {', '.join(sorted(valid))}")
        return

    message = None
    for i, a in enumerate(args):
        if a in ("--message", "-m") and i + 1 < len(args):
            message = args[i + 1]

    t = _resolve_task(tid)
    if not t:
        print(f"Task '{tid}' not found.")
        return

    payload = {"status": new_status}
    if message:
        payload["latest_status_message"] = message
    try:
        resp = patch_task(t["id"], payload)
        print(f"  {t['id'][:8]}: {t['status']} -> {resp.get('status', new_status)}")
    except Exception as e:
        print(f"  {t['id'][:8]}: FAILED -- {e}")


def cmd_scope(args):
    """Show the prompt, research question, and payload for a task — what it's actually doing."""
    from blueprint_api import api
    if not args:
        print("Usage: tasks.py scope <id_or_title>")
        return
    query = args[0]
    t = _resolve_task(query)
    if not t:
        print(f"Task '{query}' not found.")
        return
    # Fetch full task
    try:
        full = api("GET", f"/api/admin/tasks/{t['id']}")
        t = full.get("task", full)
    except Exception:
        pass

    tid = t["id"][:8]
    title = t.get("title", "?")
    status = t.get("status", "?")
    ptype = (t.get("payloads") or [{}])[0].get("type", t.get("task_type", "?"))
    ctx = t.get("context") or {}
    payloads = t.get("payloads") or []

    print(f"=== {tid} [{ptype}] {status} ===")
    print(f"Title: {title}")
    print()

    # Research question
    rq = ctx.get("research_question", "")
    if rq:
        print(f"Research Question:")
        print(f"  {rq}")
        print()

    # Scope
    scope = ctx.get("scope", "")
    if scope:
        print(f"Scope: {scope}")
        print()

    # Prompt from payload
    if payloads:
        prompt = payloads[0].get("prompt", "")
        if prompt:
            print(f"Prompt ({len(prompt)} chars):")
            # Show first 500 chars, full if -v
            lim = len(prompt) if ("-v" in args or "--verbose" in args) else 500
            print(f"  {_safe(prompt[:lim], lim)}")
            if len(prompt) > lim:
                print(f"  ... ({len(prompt) - lim} more chars, use -v for full)")
            print()

    # PR info
    pr_num = (payloads[0] if payloads else {}).get("pr_number")
    branch = (payloads[0] if payloads else {}).get("branch", "")
    base = (payloads[0] if payloads else {}).get("base_branch", "")
    if pr_num:
        print(f"PR: #{pr_num}  branch={branch}  base={base}")

    # Artifacts
    artifacts = t.get("artifacts") or {}
    if isinstance(artifacts, list):
        for art in artifacts:
            pr_url = art.get("pr_url", "")
            if pr_url:
                print(f"PR URL: {pr_url}")
    elif isinstance(artifacts, dict):
        pr_url = artifacts.get("pr_url", "")
        art_branch = artifacts.get("branch_name", "")
        if pr_url:
            print(f"PR URL: {pr_url}")
        if art_branch:
            print(f"Branch: {art_branch}")

    # Children summary
    children = _get_children(t["id"][:8])
    if children:
        print()
        by_status = {}
        for c in children:
            by_status.setdefault(c["status"], []).append(c)
        merged = sum(1 for c in children if (c.get("context") or {}).get("pr_merged"))
        print(f"Children: {len(children)} (merged={merged})")
        icons = {"QUEUED": "Q", "PROVISIONING": "P", "IN_PROGRESS": "R", "COMPLETED": "D",
                 "FAILED": "F", "CANCELLED": "X", "EVALUATING": "E"}
        for c in children:
            cptype = (c.get("payloads") or [{}])[0].get("type", "?")
            if cptype in ("review", "fix", "debug"):
                continue
            s = icons.get(c["status"], "?")
            cm = (c.get("context") or {}).get("pr_merged", False)
            c_arts = c.get("artifacts") or {}
            cpr = c_arts[0].get("pr_url", "") if isinstance(c_arts, list) and c_arts else c_arts.get("pr_url", "") if isinstance(c_arts, dict) else ""
            print(f"  [{s}] {c['id'][:8]}  {_safe(c.get('title',''), 55)}  merged={cm}")
            if cpr:
                print(f"       {cpr}")


def cmd_failures(args):
    """Analyze failed tasks — group by root cause, show actionable details."""
    from blueprint_api import api
    limit = 50
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])

    tasks = list_tasks(status="FAILED", limit=limit)
    if not tasks:
        print("No failed tasks.")
        return

    # Classify each failure
    buckets = {}  # cause -> list of tasks
    for t in tasks:
        msg = t.get("latest_status_message") or ""
        error = t.get("error_logs") or ""
        logs = t.get("status_log") or []
        ctx = t.get("context") or {}
        fc = ctx.get("failure_class", "")

        # Determine root cause from patterns.
        # Check error_logs first (more specific), then status message, then failure_class.
        cause = "UNKNOWN"
        if "API usage exhausted" in error or "API usage exhausted" in msg:
            cause = "API_EXHAUSTED"
        elif "spawn E2BIG" in error or "spawn E2BIG" in msg:
            cause = "SPAWN_E2BIG"
        elif "ZONE_RESOURCE_POOL_EXHAUSTED" in msg or "ZONE_RES" in msg:
            cause = "ZONE_EXHAUSTED"
        elif "VM provisioning failed" in msg and "400" in msg:
            cause = "VM_PROVISION_ERROR"
        elif "rollup PR creation failed" in msg:
            cause = "ROLLUP_PR_FAILED"
        elif "review not spawned (iteration cap)" in msg:
            cause = "ITERATION_CAP"
        elif "subtask(s) failed" in msg:
            cause = "SUBTASK_FAILURES"
        elif "Evaluation timeout" in msg or "did not complete within" in msg:
            cause = "EVALUATION_TIMEOUT"
        elif "Agent startup failed" in msg:
            cause = "AGENT_STARTUP_FAILURE"
        elif "No heartbeat" in msg and "since prov" in msg:
            cause = "HEARTBEAT_NEVER_STARTED"
        elif "Provision failed" in msg and "no GCP error" in msg:
            cause = "AGENT_STARTUP_FAILURE"  # VM created but agent died
        elif "Provision failed" in msg or "No heartbeat" in msg:
            cause = "HEARTBEAT_STARVATION"
        elif "git clone" in error or "git clone" in msg:
            cause = "GIT_CLONE_FAILED"
        elif "Secret Manager" in error or "secret" in error.lower():
            cause = "SECRET_MANAGER_ERROR"
        elif "RATE_LIMIT" in fc or "rate" in error.lower():
            cause = "RATE_LIMIT"
        elif "CODE_ERROR" in fc:
            cause = "CODE_ERROR"
        elif "AGENT_TIMEOUT" in fc:
            cause = "AGENT_TIMEOUT"
        elif fc:
            cause = fc

        buckets.setdefault(cause, []).append(t)

    print(f"\n=== FAILED TASKS ({len(tasks)}) ===\n")
    for cause, group in sorted(buckets.items(), key=lambda x: -len(x[1])):
        print(f"--- {cause} ({len(group)} tasks) ---")
        for t in group:
            tid = t["id"][:8]
            ttype = (t.get("payloads") or [{}])[0].get("type", "?")
            title = _safe(t.get("title", ""), 55)
            attempts = t.get("attempt_count", 0)
            parent = (t.get("parent_task_id") or "")[:8]
            msg = _safe(t.get("latest_status_message") or "", 100)
            error = _safe((t.get("error_logs") or "").split("\n")[0], 120)
            print(f"  {tid}  [{ttype:8}]  att={attempts}  parent={parent or '-':>8}  {title}")
            if error:
                print(f"    ERR: {error}")
            elif msg:
                print(f"    MSG: {msg}")

            # Show timing pattern from status_log
            logs = t.get("status_log") or []
            in_prog_times = [l for l in logs if "IN_PROGRESS" in (l.get("msg") or "") and "PROVISIONING" not in (l.get("msg") or "")]
            fail_times = [l for l in logs if "FAILED" in (l.get("msg") or "") and "Failed after" not in (l.get("msg") or "")]
            if in_prog_times and fail_times:
                # Check for instant failures (< 5s between IN_PROGRESS and FAILED)
                instant_fails = 0
                for ip in in_prog_times:
                    for ft in fail_times:
                        try:
                            ip_t = ip["t"]
                            ft_t = ft["t"]
                            # Parse ISO timestamps
                            from datetime import datetime
                            ip_dt = datetime.fromisoformat(ip_t)
                            ft_dt = datetime.fromisoformat(ft_t)
                            delta = (ft_dt - ip_dt).total_seconds()
                            if 0 < delta < 5:
                                instant_fails += 1
                        except (ValueError, KeyError):
                            pass
                if instant_fails:
                    print(f"    PATTERN: {instant_fails} instant failure(s) (<5s after start)")
        print()

    # Summary
    print("=== SUMMARY ===")
    for cause, group in sorted(buckets.items(), key=lambda x: -len(x[1])):
        requeue_candidates = [t for t in group if cause not in ("CODE_ERROR",)]
        print(f"  {cause}: {len(group)} tasks" + (f" ({len(requeue_candidates)} requeueable)" if requeue_candidates else ""))
    print()

    # Actionable suggestions
    if "GIT_CLONE_FAILED" in buckets:
        branch_set = set()
        for t in buckets["GIT_CLONE_FAILED"]:
            bb = (t.get("payloads") or [{}])[0].get("base_branch", "?")
            branch_set.add(bb)
        print(f"  ACTION [GIT_CLONE_FAILED]: Check if branch(es) exist: {', '.join(branch_set)}")
        print(f"         Requeue with: tasks.py requeue " + " ".join(t["id"][:8] for t in buckets["GIT_CLONE_FAILED"]))
    if "HEARTBEAT_NEVER_STARTED" in buckets:
        print(f"  ACTION [HEARTBEAT_NEVER_STARTED]: Agent process failed to start on VM.")
        print(f"         Check VM image, systemd journal, or Secret Manager provisioning.")
        print(f"         Requeue with: tasks.py requeue " + " ".join(t["id"][:8] for t in buckets["HEARTBEAT_NEVER_STARTED"]))
    if "AGENT_STARTUP_FAILURE" in buckets:
        accts = set()
        for t in buckets["AGENT_STARTUP_FAILURE"]:
            ba = (t.get("context") or {}).get("blacklisted_accounts") or []
            accts.update(a[:8] for a in ba)
            a = (t.get("context") or {}).get("max_plan_account_id", "")
            if a:
                accts.add(a[:8])
        print(f"  ACTION [AGENT_STARTUP_FAILURE]: VM provisioned OK but agent crashed before claiming task.")
        print(f"         Likely cause: API key exhausted, startup script error, or missing secrets.")
        print(f"         Accounts involved: {', '.join(sorted(accts)) or '?'}")
        serial = None
        for t in buckets["AGENT_STARTUP_FAILURE"]:
            s = (t.get("context") or {}).get("last_serial_log")
            if s:
                serial = s
                break
        if serial:
            print(f"         Last serial log (tail): {_safe(serial, 200)}")
        print(f"         Check: ops.py pool, ops.py accounts. Requeue: tasks.py requeue " + " ".join(t["id"][:8] for t in buckets["AGENT_STARTUP_FAILURE"]))
    if "HEARTBEAT_STARVATION" in buckets:
        print(f"  ACTION [HEARTBEAT_STARVATION]: Agents died mid-work (OOM? GCP preemption?).")
        print(f"         Check for concurrent failures = systemic event (deploy, zone issue).")
        print(f"         Requeue with: tasks.py requeue " + " ".join(t["id"][:8] for t in buckets["HEARTBEAT_STARVATION"]))
    if "API_EXHAUSTED" in buckets:
        accts = set()
        for t in buckets["API_EXHAUSTED"]:
            m = t.get("latest_status_message") or ""
            if "acct:" in m:
                accts.add(m.split("acct:")[1].split(")")[0].split(",")[0].strip()[:12])
        print(f"  ACTION [API_EXHAUSTED]: Claude API usage exhausted on {len(accts) or '?'} account(s).")
        print(f"         Check account billing/quotas. Disable exhausted accounts with: ops.py accounts --disable <id>")
        print(f"         Requeue after quota reset: tasks.py requeue " + " ".join(t["id"][:8] for t in buckets["API_EXHAUSTED"]))
    if "SPAWN_E2BIG" in buckets:
        print(f"  ACTION [SPAWN_E2BIG]: Agent process args/env too large for execve.")
        print(f"         Likely prompt or context payload exceeds OS limit. Check task payloads.")
    if "ZONE_EXHAUSTED" in buckets:
        zones = set()
        for t in buckets["ZONE_EXHAUSTED"]:
            m = t.get("latest_status_message") or ""
            for tok in m.split():
                if tok.startswith("us-") or tok.startswith("europe-") or tok.startswith("asia-"):
                    zones.add(tok.rstrip(",.;:"))
        print(f"  ACTION [ZONE_EXHAUSTED]: GCP zone(s) out of capacity: {', '.join(zones) or '?'}.")
        print(f"         Requeue (will try another zone): tasks.py requeue " + " ".join(t["id"][:8] for t in buckets["ZONE_EXHAUSTED"]))
    if "VM_PROVISION_ERROR" in buckets:
        print(f"  ACTION [VM_PROVISION_ERROR]: GCP Compute API returned 400 — bad request or quota issue.")
        print(f"         Check: ops.py zones, ops.py pool. Requeue: tasks.py requeue " + " ".join(t["id"][:8] for t in buckets["VM_PROVISION_ERROR"]))
    if "ROLLUP_PR_FAILED" in buckets:
        print(f"  ACTION [ROLLUP_PR_FAILED]: Subtasks succeeded but rollup PR creation hit 500.")
        print(f"         Likely merge conflicts or GitHub API issue. Try: tasks.py trigger-rollup <parent_id>")
    if "ITERATION_CAP" in buckets:
        print(f"  ACTION [ITERATION_CAP]: PR created but review/fix cycle hit iteration cap.")
        print(f"         These PRs exist and may be mergeable. Review manually or raise caps.")
    if "EVALUATION_TIMEOUT" in buckets:
        print(f"  ACTION [EVALUATION_TIMEOUT]: Parent research tasks timed out waiting for children.")
        print(f"         Children likely failed. Fix children first, then requeue parents.")
    if "SUBTASK_FAILURES" in buckets:
        print(f"  ACTION [SUBTASK_FAILURES]: Parent tasks failed because all children failed.")
        print(f"         Diagnose child failures first. Parent IDs: " + " ".join(t["id"][:8] for t in buckets["SUBTASK_FAILURES"]))


def cmd_health(args):
    """Check system health: autonomy state, deploy status, settings, circuit breakers."""
    from blueprint_api import api

    # Fetch all endpoints in sequence (can't parallelize in sync code)
    settings = {}
    try:
        settings = api("GET", "/api/admin/settings")
    except Exception as e:
        print(f"  Settings fetch failed: {e}")

    autonomy = {}
    try:
        autonomy = api("GET", "/api/admin/autonomous-status")
    except Exception as e:
        print(f"  Autonomy status fetch failed: {e}")

    deploy = {}
    try:
        deploy = api("GET", "/api/admin/deploy-state")
    except Exception as e:
        print(f"  Deploy state fetch failed: {e}")

    zone_health = {}
    try:
        zone_health = api("GET", "/api/admin/zone-health")
    except Exception:
        pass

    print("=== SYSTEM HEALTH ===\n")

    # Settings
    if settings:
        print("--- Runtime Settings ---")
        for key in ["max_concurrent_vms", "max_retries", "max_autonomous_tasks_per_hour",
                     "autonomous_research_interval_minutes", "autonomous_direction_cooldown_minutes",
                     "min_research_interval_hours", "autonomous_research_model",
                     "autonomous_research_effort"]:
            val = settings.get(key, "?")
            print(f"  {key}: {val}")
        print()

    # Autonomy
    if autonomy:
        print("--- Autonomy State ---")
        projects = autonomy.get("projects", [])
        for p in projects:
            print(f"  Project: {p.get('project_name', '?')}")
            print(f"    enabled: {p.get('autonomous_enabled')}")
            print(f"    consecutive_failures: {p.get('consecutive_failures', 0)}")
            print(f"    tasks_spawned_last_hour: {p.get('tasks_spawned_last_hour', 0)}")
            print(f"    last_research_at: {p.get('last_research_at', 'never')}")
        print()

    # Deploy
    if deploy:
        print("--- Deploy State ---")
        for key in ["status", "current_revision", "deployed_at",
                     "pause_reason", "paused_at", "max_concurrent_vms",
                     "failure_rate", "rollback_count"]:
            val = deploy.get(key)
            if val is not None:
                print(f"  {key}: {val}")
        ext = deploy.get("external_health", {})
        if ext:
            failed = ext.get("failed_deps", [])
            if failed:
                print(f"  UNHEALTHY deps: {', '.join(failed)}")
            else:
                print(f"  external_health: all OK")
        monitoring = deploy.get("monitoring_window", {})
        if monitoring:
            pct = monitoring.get("percent", 0)
            elapsed = monitoring.get("elapsed_seconds", 0)
            total = monitoring.get("total_seconds", 0)
            print(f"  monitoring_window: {elapsed}s / {total}s ({pct}%)")
        print()

    # Zone health
    if zone_health:
        zones = zone_health if isinstance(zone_health, dict) else {}
        unhealthy = {z: d for z, d in zones.items()
                     if isinstance(d, dict) and d.get("status") != "healthy"}
        if unhealthy:
            print("--- Zone Health (unhealthy) ---")
            for z, d in unhealthy.items():
                print(f"  {z}: {d.get('status')} (failures={d.get('consecutive_failures', 0)}, cooldown={d.get('cooldown_active')})")
            print()

    # Task counts
    print("--- Task Counts ---")
    for st in ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "FAILED"]:
        all_t = list_tasks(status=st, limit=200)
        print(f"  {st}: {len(all_t)}")


def cmd_directions(args):
    """Show research directions for a project. Use -v for full text."""
    from blueprint_api import api, get_project_id
    verbose = "-v" in args or "--verbose" in args
    clean_args = [a for a in args if not a.startswith("-")]
    pid = clean_args[0] if clean_args else get_project_id()
    data = api("GET", f"/api/admin/projects/{pid}/directions")
    dirs = data.get("directions", data) if isinstance(data, dict) else data
    if not dirs:
        print("No research directions configured.")
        return
    print(f"=== RESEARCH DIRECTIONS ({len(dirs)}) ===")
    for d in dirs:
        prio = d.get("priority", "?")
        full_text = d.get("text") or d.get("summary") or "?"
        did = d.get("id") or "?"
        status = d.get("status", "active")
        saturated = d.get("saturated", False)
        sat_str = " [SATURATED]" if saturated else ""
        status_str = f" [{status}]" if status != "active" else ""
        history = d.get("research_history") or []
        hist_str = f"  ({len(history)} cycles)" if history else ""
        if verbose:
            print(f"\n  [{prio:6}] {did}{sat_str}{status_str}{hist_str}")
            print(f"  {full_text}")
            notes = d.get("notes") or []
            if notes:
                print(f"  Notes ({len(notes)}):")
                for n in notes[-3:]:
                    print(f"    • {_safe(n.get('text', ''), 200)}")
        else:
            print(f"  [{prio:6}] {did[:8]}  {_safe(full_text, 120)}{sat_str}{hist_str}")


def cmd_reset_iteration_cap(args):
    """Reset the review-fix iteration cap for a stuck rollup/parking task.

    Archives FAILED review/fix tasks on the PR (drops them from the global
    count) and resets the per-chain iteration counter to 0. Use when the cap
    tripped on infra failures rather than real review iterations.

    Usage: tasks.py reset-iteration-cap <task_id>
    """
    from blueprint_api import api
    if not args:
        print("Usage: tasks.py reset-iteration-cap <task_id>")
        return
    tid = args[0]
    result = api("POST", f"/api/admin/tasks/{tid}/reset-iteration-cap")
    print(f"PR #{result.get('pr_number')}: archived {result.get('archived_count')} failed review/fix tasks, "
          f"per-chain iteration {result.get('previous_per_chain_iteration')}->0"
          + (" (cleared requires_approval)" if result.get('requires_approval_cleared') else ""))


def cmd_unhold_direction(args):
    """Clear a direction's consecutive-failure hold.

    Usage: tasks.py unhold-direction <direction_id>
    """
    from blueprint_api import api, get_project_id
    if not args:
        print("Usage: tasks.py unhold-direction <direction_id>")
        return
    did = args[0]
    pid = get_project_id()
    result = api("POST", f"/api/admin/projects/{pid}/directions/{did}/unhold")
    was_held = result.get("was_held", False)
    if was_held:
        print(f"Unheld direction {did}")
    else:
        print(f"Direction {did} was not held (idempotent)")


def cmd_add_direction(args):
    """Add a new research direction.

    Usage: tasks.py add-direction "direction text" [--priority high|medium|low]
           tasks.py add-direction --text "direction text" --priority high
    """
    from blueprint_api import api, get_project_id
    if not args:
        print("Usage: tasks.py add-direction 'direction text' [--priority high|medium|low]")
        return
    text = None
    priority = "medium"
    for i, a in enumerate(args):
        if a == "--priority" and i + 1 < len(args):
            priority = args[i + 1]
        elif a == "--text" and i + 1 < len(args):
            text = args[i + 1]
    # If no --text flag, first non-flag arg is the text
    if text is None:
        positional = [a for a in args if not a.startswith("--") and a not in (priority,)]
        text = positional[0] if positional else None
    if not text:
        print("Error: direction text required")
        return
    pid = get_project_id()
    result = api("POST", f"/api/admin/projects/{pid}/directions", {"text": text, "priority": priority})
    d = result.get("direction", {})
    print(f"Added direction {d.get('id', '?')} [{priority}]: {text[:80]}")


def cmd_update_direction(args):
    """Update a direction's text, priority, or status.

    Usage:
        tasks.py update-direction <direction_id> [--text "new text"] [--priority high|medium|low] [--status active|paused|completed]
        tasks.py update-direction <direction_id> --reset-context        — wipe research_context
        tasks.py update-direction <direction_id> --reset-history        — wipe research_history
        tasks.py update-direction <direction_id> --reset-agent-notes    — remove agent-added notes (keep user notes)
        tasks.py update-direction all --reset-all                       — full blank slate (context + history + agent notes)
    """
    from blueprint_api import api, get_project_id
    if not args:
        print("Usage: tasks.py update-direction <direction_id> [--text '...'] [--priority ...] [--status ...] [--reset-context] [--reset-history] [--reset-agent-notes] [--reset-all]")
        return
    did = args[0]
    reset_all = "--reset-all" in args

    def _build_reset_patch(direction_data):
        """Build patch dict for reset flags, optionally using direction data for note filtering."""
        patch = {}
        if "--reset-context" in args or reset_all:
            patch["research_context"] = "## Established Findings\n\n## Failed Approaches\n\n## Open Questions\n"
        if "--reset-history" in args or reset_all:
            patch["research_history"] = []
        if "--reset-agent-notes" in args or reset_all:
            notes = direction_data.get("notes", []) if direction_data else []
            # Keep only user-added notes (no added_by_task field)
            user_notes = [n for n in notes if not n.get("added_by_task")]
            patch["notes"] = user_notes
        return patch

    # Handle "all" target for bulk resets
    if did.lower() == "all":
        pid = get_project_id()
        dirs_data = api("GET", f"/api/admin/projects/{pid}/directions")
        all_dirs = dirs_data if isinstance(dirs_data, list) else dirs_data.get("directions", [])
        for d in all_dirs:
            d_id = d.get("id", "?")
            d_name = (d.get("text") or "?")[:50]
            patch = _build_reset_patch(d)
            if patch:
                api("PATCH", f"/api/admin/projects/{pid}/directions/{d_id}", patch)
                actions = []
                if "research_context" in patch:
                    actions.append("context")
                if "research_history" in patch:
                    actions.append("history")
                if "notes" in patch:
                    kept = len(patch["notes"])
                    removed = len(d.get("notes", [])) - kept
                    actions.append(f"notes (-{removed}, kept {kept} user)")
                print(f"  Reset {d_id[:12]} ({d_name}): {', '.join(actions)}")
        return

    patch = {}
    for i, a in enumerate(args):
        if a == "--text" and i + 1 < len(args):
            patch["text"] = args[i + 1]
        if a == "--priority" and i + 1 < len(args):
            patch["priority"] = args[i + 1]
        if a == "--status" and i + 1 < len(args):
            patch["status"] = args[i + 1]
        if a == "--reset-context" or reset_all:
            patch["research_context"] = "## Established Findings\n\n## Failed Approaches\n\n## Open Questions\n"
        if a == "--reset-history" or reset_all:
            patch["research_history"] = []
        if a == "--reset-agent-notes" or reset_all:
            # Need direction data for filtering — fetch it
            pid_tmp = get_project_id()
            dirs_tmp = api("GET", f"/api/admin/projects/{pid_tmp}/directions")
            all_tmp = dirs_tmp if isinstance(dirs_tmp, list) else dirs_tmp.get("directions", [])
            for dt in all_tmp:
                if dt.get("id") == did:
                    user_notes = [n for n in dt.get("notes", []) if not n.get("added_by_task")]
                    patch["notes"] = user_notes
                    break
    if not patch:
        print("Nothing to update. Use --text, --priority, or --status.")
        return
    pid = get_project_id()
    result = api("PATCH", f"/api/admin/projects/{pid}/directions/{did}", patch)
    print(f"Updated {did}: {json.dumps(patch)}")
    # Show the updated direction
    updated = result.get("direction", {})
    if updated:
        print(f"  New text: {updated.get('text', '?')}")


def cmd_autonomy_debug(args):
    """Show why autonomy is or isn't spawning — blocking guards, eligible directions."""
    from blueprint_api import api, get_project_id
    pid = args[0] if args and not args[0].startswith("-") else get_project_id()
    data = api("GET", f"/api/admin/projects/{pid}/autonomy-debug")

    print("=== AUTONOMY DEBUG ===")
    blocked = data.get('spawn_blocked')
    if blocked is True:
        print("  can_spawn: NO (blocked)")
    elif blocked is False:
        print("  can_spawn: YES — no guards; next tick will try to spawn")
    else:
        print("  can_spawn: ?")

    guards = data.get('blocking_guards', [])
    if guards:
        print(f"\n--- Blocking Guards ({len(guards)}) ---")
        for g in guards:
            print(f"  {g.get('guard', '?')}: {g.get('reason', '?')}")
            for k, v in g.items():
                if k not in ('guard', 'reason'):
                    print(f"    {k}: {v}")
    else:
        print("  No blocking guards — autonomy should spawn on next tick.")

    cb = data.get('circuit_breaker', {})
    if cb:
        print(f"\n--- Circuit Breaker ---")
        print(f"  state={cb.get('state')}  failures={cb.get('failure_count')}  successes={cb.get('success_count')}")
        if cb.get('cooldown_ends_at'):
            print(f"  cooldown_until={cb['cooldown_ends_at']}")

    dirs = data.get('directions', [])
    if dirs:
        eligible = [d for d in dirs if d.get('is_eligible')]
        print(f"\n--- Directions ({len(eligible)}/{len(dirs)} eligible) ---")
        for d in dirs:
            did = (d.get('id') or '?')[:10]
            text = (d.get('text') or '?')[:70]
            markers = []
            if d.get('seconds_until_eligible'):
                secs = d['seconds_until_eligible']
                markers.append(f"cooldown {secs // 60}m")
            if d.get('busy_reason'):
                markers.append("busy")
            if d.get('is_eligible'):
                markers.append("eligible")
            tag = f" [{', '.join(markers)}]" if markers else ""
            print(f"  {did}  {text}{tag}")

    spawn = data.get('spawn_readiness', {})
    if spawn:
        print(f"\n--- Spawn Readiness ---")
        for k, v in spawn.items():
            print(f"  {k}: {v}")


def cmd_archive(args):
    """Archive old completed/failed tasks."""
    from blueprint_api import api, all_tasks_by_status
    if '--pipelines' in args:
        # Archive fully-terminal pipelines: parent + all children in COMPLETED/CANCELLED
        TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED'}
        all_tasks = all_tasks_by_status(None, limit=1000)
        non_archived = [t for t in all_tasks if not t.get('archived')]

        # Build parent → children map
        children_by_parent = {}
        for t in non_archived:
            pid = t.get('parent_task_id')
            if pid:
                children_by_parent.setdefault(pid, []).append(t)

        # Find top-level parents (no parent themselves) that are terminal
        top_parents = [t for t in non_archived if not t.get('parent_task_id') and t['status'] in TERMINAL]

        to_archive = []
        for parent in top_parents:
            children = children_by_parent.get(parent['id'], [])
            all_terminal = all(c['status'] in TERMINAL for c in children)
            if all_terminal:
                to_archive.append(parent['id'])
                to_archive.extend(c['id'] for c in children)

        if not to_archive:
            print("No fully-terminal pipelines to archive.")
            return
        pipeline_count = sum(1 for p in top_parents if p['id'] in to_archive)
        result = api("POST", "/api/admin/tasks/bulk-archive", {"task_ids": to_archive})
        print(f"Archived {result.get('archived_count', '?')} tasks across {pipeline_count} pipelines.")
        return

    filter_type = 'terminal'  # default: archive all terminal tasks
    for i, a in enumerate(args):
        if a == '--completed':
            filter_type = 'completed'
    result = api("POST", "/api/admin/tasks/bulk-archive", {"filter": filter_type})
    print(f"Archived {result.get('archived_count', '?')} tasks (filter={filter_type}).")


def cmd_payload(args):
    """Dump the full raw payload and context JSON for a task — everything the agent receives."""
    from blueprint_api import api
    if not args:
        print("Usage: tasks.py payload <id_or_title>")
        return
    query = args[0]
    t = _resolve_task(query)
    if not t:
        print(f"Task '{query}' not found.")
        return
    try:
        full = api("GET", f"/api/admin/tasks/{t['id']}")
        t = full.get("task", full)
    except Exception:
        pass

    tid = t["id"][:8]
    ptype = (t.get("payloads") or [{}])[0].get("type", "?")
    print(f"=== {tid} [{ptype}] {t.get('status','?')} ===")
    print(f"Title: {t.get('title','?')}")
    print()

    # Full payloads
    payloads = t.get("payloads") or []
    if payloads:
        print("=== PAYLOADS ===")
        print(json.dumps(payloads, indent=2, default=str))
        print()

    # Full context
    ctx = t.get("context") or {}
    if ctx:
        print("=== CONTEXT ===")
        print(json.dumps(ctx, indent=2, default=str))


def cmd_research(args):
    """Show recent research results — findings, proposed tasks, and outcomes.

    Usage:
        tasks.py research                — last 5 completed research tasks
        tasks.py research --limit 10     — last N
        tasks.py research <id>           — detailed view of one research task
        tasks.py research -v             — verbose (show full finding prompts)
    """
    from blueprint_api import api

    verbose = "-v" in args or "--verbose" in args
    limit = 5
    query = None
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        elif not a.startswith("-"):
            query = a

    # Single research task detail
    if query:
        t = _resolve_task(query)
        if not t:
            print(f"Task '{query}' not found.")
            return
        try:
            full = api("GET", f"/api/admin/tasks/{t['id']}")
            t = full.get("task", full)
        except Exception:
            pass
        _print_research_detail(t, verbose)
        return

    # List recent research tasks
    all_completed = list_tasks(status="COMPLETED", limit=100)
    all_evaluating = list_tasks(status="EVALUATING", limit=50)
    all_failed = list_tasks(status="FAILED", limit=50)
    research_tasks = [t for t in all_completed + all_evaluating + all_failed
                      if t.get("task_type") == "research"][:limit]

    if not research_tasks:
        print("No recent research tasks found.")
        return

    for t in research_tasks:
        try:
            full = api("GET", f"/api/admin/tasks/{t['id']}")
            t = full.get("task", full)
        except Exception:
            pass
        _print_research_detail(t, verbose)
        print()


def _print_research_detail(t, verbose=False):
    """Print a research task's findings and child task outcomes."""
    tid = t["id"][:8]
    status = t.get("status", "?")
    title = t.get("title", "?")
    icons = {"COMPLETED": "ok", "EVALUATING": "eval", "FAILED": "FAIL", "QUEUED": "wait", "IN_PROGRESS": "run"}
    icon = icons.get(status, status)
    msg = (t.get("latest_status_message") or "")[:120]

    print(f"=== {tid} [{icon}] {_safe(title, 80)} ===")
    print(f"  Status: {msg}")

    # Direction context
    ctx = t.get("context") or {}
    direction = ctx.get("direction_summary") or ctx.get("research_question", "")
    if direction:
        print(f"  Direction: {_safe(direction, 120)}")

    # Findings from plan_output
    findings = t.get("plan_output") or []
    children = _get_children(t["id"][:8])

    if not findings:
        # Reconstruct from children for historical tasks missing plan_output
        if children:
            impl_children = [c for c in children
                             if c.get("task_type") not in ("review", "fix", "debug", "smoketest")]
            if impl_children:
                print(f"\n  Findings: (not stored — reconstructed from {len(impl_children)} children)")
                for i, c in enumerate(impl_children):
                    ctitle = _safe(c.get("title", ""), 80)
                    ctype = c.get("task_type", "?")
                    print(f"    {i+1}. [{ctype}] {ctitle}")
            else:
                print("  Findings: (none stored)")
        else:
            print("  Findings: (none stored)")

    if findings:
        print(f"\n  Findings ({len(findings)}):")
        for i, f in enumerate(findings):
            if isinstance(f, str):
                print(f"    {i+1}. {_safe(f, 100)}")
                continue
            ftitle = f.get("title", "?")
            ftype = f.get("task_type") or f.get("type", "?")
            severity = f.get("severity", "")
            sev_tag = f" [{severity}]" if severity else ""
            print(f"    {i+1}. [{ftype}]{sev_tag} {_safe(ftitle, 80)}")

            # Show prompt preview or full
            prompt = f.get("prompt", "")
            if prompt:
                if verbose:
                    for line in prompt.split("\n")[:15]:
                        print(f"       {_safe(line, 120)}")
                else:
                    print(f"       {_safe(prompt, 120)}")

    # Children outcomes
    if children:
        # Filter out review/fix/debug children for the summary
        impl_children = [c for c in children
                         if c.get("task_type") not in ("review", "fix", "debug", "smoketest")]
        if not impl_children:
            impl_children = children

        merged = sum(1 for c in impl_children if (c.get("context") or {}).get("pr_merged"))
        failed = sum(1 for c in impl_children if c["status"] == "FAILED")
        completed = sum(1 for c in impl_children if c["status"] == "COMPLETED")

        print(f"\n  Children: {len(impl_children)} total, {merged} merged, {completed} completed, {failed} failed")
        for c in impl_children:
            cid = c["id"][:8]
            cstatus = c.get("status", "?")
            ctitle = _safe(c.get("title", ""), 65)
            c_arts = c.get("artifacts") or {}
            cpr = ""
            if isinstance(c_arts, list) and c_arts:
                cpr = c_arts[0].get("pr_url", "")
            elif isinstance(c_arts, dict):
                cpr = c_arts.get("pr_url", "")
            pr_tag = f" PR: {cpr.split('/')[-1]}" if cpr and "/" in cpr else ""
            cm = " MERGED" if (c.get("context") or {}).get("pr_merged") else ""
            print(f"    [{cstatus:10}] {cid}  {ctitle}{pr_tag}{cm}")


def cmd_playbook(args):
    """List, show, or delete playbook entries.

    Usage:
        tasks.py playbook                  — list all entries
        tasks.py playbook delete <key>     — delete a specific entry
        tasks.py playbook prune <pattern>  — delete entries matching pattern (case-insensitive)
    """
    from blueprint_api import api, get_project_id
    pid = get_project_id()
    url = f"/api/admin/projects/{pid}/playbook"

    if not args or args[0] not in ("delete", "prune"):
        data = api("GET", url)
        entries = data.get("entries", {})
        if not entries:
            print("No playbook entries.")
            return
        print(f"Playbook entries ({len(entries)}):")
        for k, v in sorted(entries.items()):
            content = v if isinstance(v, str) else v.get("content", "")
            print(f"  {k}: {_safe(content, 120)}")
    elif args[0] == "delete":
        if len(args) < 2:
            print("Usage: tasks.py playbook delete <key>")
            return
        key = args[1]
        api("DELETE", f"{url}/{key}")
        print(f"Deleted: {key}")
    elif args[0] == "prune":
        if len(args) < 2:
            print("Usage: tasks.py playbook prune <pattern>")
            return
        import re
        pattern = re.compile(args[1], re.IGNORECASE)
        data = api("GET", url)
        entries = data.get("entries", {})
        matched = []
        for k, v in entries.items():
            content = v if isinstance(v, str) else v.get("content", "")
            if pattern.search(k) or pattern.search(content):
                matched.append((k, content))
        if not matched:
            print(f"No entries matching '{args[1]}'.")
            return
        print(f"Entries matching '{args[1]}' ({len(matched)}):")
        for k, content in matched:
            print(f"  {k}: {_safe(content, 120)}")
        confirm = input(f"\nDelete {len(matched)} entries? [y/N] ")
        if confirm.lower() == "y":
            for k, _ in matched:
                api("DELETE", f"{url}/{k}")
                print(f"  Deleted: {k}")
            print("Done.")
        else:
            print("Cancelled.")


COMMANDS = {
    "list": cmd_list,
    "show": cmd_show,
    "scope": cmd_scope,
    "payload": cmd_payload,
    "tree": cmd_tree,
    "children": cmd_children,
    "diagnose": cmd_diagnose,
    "failures": cmd_failures,
    "health": cmd_health,
    "complete": cmd_complete,
    "set-status": cmd_set_status,
    "requeue": cmd_requeue,
    "cancel": cmd_cancel,
    "cancel-all": cmd_cancel_all,
    "evaluating": cmd_evaluating,
    "requeue-ci": cmd_requeue_ci,
    "trigger-rollup": cmd_trigger_rollup,
    "directions": cmd_directions,
    "add-direction": cmd_add_direction,
    "update-direction": cmd_update_direction,
    "unhold-direction": cmd_unhold_direction,
    "reset-iteration-cap": cmd_reset_iteration_cap,
    "autonomy-debug": cmd_autonomy_debug,
    "archive": cmd_archive,
    "research": cmd_research,
    "playbook": cmd_playbook,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: tasks.py <command> [args...]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
