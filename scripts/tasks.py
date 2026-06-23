"""Unified task management CLI.

Several subcommands have been superseded by ``python scripts/blueprint.py``
and are now deprecated stubs that print a pointer and exit non-zero:

    DEPRECATED — use blueprint.py instead:
        list    → python scripts/blueprint.py tasks list [--status STATUS] [--limit N]
        show    → python scripts/blueprint.py tasks show <task_id>
        requeue → python scripts/blueprint.py tasks list (find id), then PATCH via API
        cancel  → python scripts/blueprint.py tasks list (find id), then PATCH via API

Active subcommands (not covered by blueprint.py):
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
from blueprint_api import list_tasks, all_tasks_by_status, patch_task, fmt_task, api


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


def cmd_list(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks list"""
    print("DEPRECATED: use python scripts/blueprint.py tasks list [--status STATUS] [--limit N]")
    sys.exit(1)


def cmd_show(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks show <task_id>"""
    print("DEPRECATED: use python scripts/blueprint.py tasks show <task_id>")
    sys.exit(1)


def cmd_tree(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks tree <parent_id>"""
    print(
        "DEPRECATED: tasks.py tree is superseded.\n"
        "Use: python scripts/blueprint.py tasks tree <parent_id> [--all]"
    )
    sys.exit(1)




def cmd_children(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks tree <parent_id>"""
    print(
        "DEPRECATED: tasks.py children is superseded.\n"
        "Use: python scripts/blueprint.py tasks tree <parent_id> [--all]"
    )
    sys.exit(1)


def cmd_requeue(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks requeue <task_id>"""
    print(
        "DEPRECATED: tasks.py requeue is superseded.\n"
        "Use: python scripts/blueprint.py tasks requeue <task_id>"
    )
    sys.exit(1)


def cmd_cancel(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks cancel <task_id>"""
    print(
        "DEPRECATED: tasks.py cancel is superseded.\n"
        "Use: python scripts/blueprint.py tasks cancel <task_id>"
    )
    sys.exit(1)


def cmd_cancel_all(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks cancel-all"""
    print(
        "DEPRECATED: tasks.py cancel-all is superseded.\n"
        "Use: python scripts/blueprint.py tasks cancel-all [--status STATUS] "
        "[--category CATEGORY] [--yes]"
    )
    sys.exit(1)


def cmd_evaluating(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks evaluating"""
    print(
        "DEPRECATED: tasks.py evaluating is superseded.\n"
        "Use: python scripts/blueprint.py tasks evaluating [-v]"
    )
    sys.exit(1)


def cmd_requeue_ci(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks requeue-ci"""
    print(
        "DEPRECATED: tasks.py requeue-ci is superseded.\n"
        "Use: python scripts/blueprint.py tasks requeue-ci [--yes]"
    )
    sys.exit(1)


def cmd_trigger_rollup(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks trigger-rollup"""
    print(
        "DEPRECATED: tasks.py trigger-rollup is superseded.\n"
        "Use: python scripts/blueprint.py tasks trigger-rollup <parent_id> [--yes]"
    )
    sys.exit(1)


def cmd_diagnose(args):
    """DEPRECATED — use: python scripts/blueprint.py diagnostics"""
    print("DEPRECATED: use python scripts/blueprint.py diagnostics")
    sys.exit(1)


def cmd_complete(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks complete"""
    print(
        "DEPRECATED: tasks.py complete is superseded.\n"
        "Use: python scripts/blueprint.py tasks complete <task_id> [--message MSG] [--yes]"
    )
    sys.exit(1)


def cmd_set_status(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks set-status"""
    print(
        "DEPRECATED: tasks.py set-status is superseded.\n"
        "Use: python scripts/blueprint.py tasks set-status <task_id> <STATUS> "
        "[--message MSG] [--yes]"
    )
    sys.exit(1)


def cmd_scope(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks scope <task_id>"""
    print(
        "DEPRECATED: tasks.py scope is superseded.\n"
        "Use: python scripts/blueprint.py tasks scope <task_id> [-v]"
    )
    sys.exit(1)


def cmd_failures(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks failures"""
    print(
        "DEPRECATED: tasks.py failures is superseded.\n"
        "Use: python scripts/blueprint.py tasks failures [--limit N]"
    )
    sys.exit(1)


def _cmd_failures_impl(args):
    """Analyze failed tasks — group by root cause, show actionable details."""
    from blueprint_api import api
    limit = 50
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])

    # The admin API caps each page at 500 rows; paginate to honour --limit.
    tasks = []
    offset = 0
    while len(tasks) < limit:
        batch = list_tasks(status="FAILED", limit=min(500, limit - len(tasks)), offset=offset)
        if not batch:
            break
        tasks.extend(batch)
        offset += len(batch)
        if len(batch) < 500:
            break
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
        elif "SCOPE_EMPTY" in msg or "SCOPE_EMPTY" in error:
            cause = "SCOPE_EMPTY"
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


def cmd_health(_args):
    """DEPRECATED — use: python scripts/blueprint.py health deploy"""
    print(
        "DEPRECATED: tasks.py health is superseded.\n"
        "  Deploy health:  python scripts/blueprint.py health deploy\n"
        "  Autonomy state: python scripts/blueprint.py health autonomy\n"
        "  Settings:       python scripts/blueprint.py settings"
    )
    sys.exit(1)


def cmd_directions(args):
    """DEPRECATED — use: python scripts/blueprint.py directions list"""
    print(
        "DEPRECATED: tasks.py directions is superseded.\n"
        "  Use: python scripts/blueprint.py directions [list|show|add|update|unhold]"
    )
    import sys; sys.exit(1)


def cmd_reset_iteration_cap(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks reset-iteration-cap"""
    print(
        "DEPRECATED: tasks.py reset-iteration-cap is superseded.\n"
        "Use: python scripts/blueprint.py tasks reset-iteration-cap <task_id> [--yes]"
    )
    sys.exit(1)


def cmd_unhold_direction(args):
    """DEPRECATED — use: python scripts/blueprint.py directions unhold <direction_id>"""
    print(
        "DEPRECATED: tasks.py unhold-direction is superseded.\n"
        "  Use: python scripts/blueprint.py directions unhold <direction_id>"
    )
    import sys; sys.exit(1)


def cmd_add_direction(args):
    """DEPRECATED — use: python scripts/blueprint.py directions add "text" [--priority ...]"""
    print(
        "DEPRECATED: tasks.py add-direction is superseded.\n"
        "  Use: python scripts/blueprint.py directions add \"text\" [--priority high|medium|low]"
    )
    import sys; sys.exit(1)


def cmd_update_direction(args):
    """DEPRECATED — use: python scripts/blueprint.py directions update <id> [--text ...] [--priority ...] [--status ...]"""
    print(
        "DEPRECATED: tasks.py update-direction is superseded.\n"
        "  Use: python scripts/blueprint.py directions update <direction_id> "
        "[--text ...] [--priority ...] [--status ...] [--reset-context] [--reset-history] [--reset-agent-notes] [--reset-all]"
    )
    import sys; sys.exit(1)


def cmd_autonomy_debug(_args):
    """DEPRECATED — use: python scripts/blueprint.py health autonomy"""
    print(
        "DEPRECATED: tasks.py autonomy-debug is superseded.\n"
        "  Use: python scripts/blueprint.py health autonomy"
    )
    sys.exit(1)


def cmd_archive(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks archive"""
    print(
        "DEPRECATED: tasks.py archive is superseded.\n"
        "Use: python scripts/blueprint.py tasks archive [--completed] [--pipelines] [--yes]"
    )
    sys.exit(1)


def cmd_payload(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks scope <task_id>"""
    print(
        "DEPRECATED: tasks.py payload is superseded.\n"
        "Use: python scripts/blueprint.py tasks scope <task_id> [-v]"
    )
    sys.exit(1)


def cmd_research(_args):
    """DEPRECATED — use: python scripts/blueprint.py tasks research"""
    print(
        "DEPRECATED: tasks.py research is superseded.\n"
        "Use: python scripts/blueprint.py tasks research [<task_id>] [--limit N] [-v]"
    )
    sys.exit(1)


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
    """DEPRECATED — use: python scripts/blueprint.py playbook"""
    print(
        "DEPRECATED: use python scripts/blueprint.py playbook\n"
        "  List:   python scripts/blueprint.py playbook\n"
        "  Delete: python scripts/blueprint.py playbook delete <key>\n"
        "  Prune:  python scripts/blueprint.py playbook prune <pattern> [--yes]"
    )
    sys.exit(1)


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
