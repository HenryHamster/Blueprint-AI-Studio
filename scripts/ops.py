"""Blueprint operations CLI — deploy state, pool health, settings, and system ops.

Usage:
    python scripts/ops.py status                    — full system overview
    python scripts/ops.py resume                    — resume pipeline (unpause)
    python scripts/ops.py pause                     — pause pipeline
    python scripts/ops.py repair                    — repair stuck tasks, orphaned parents
    python scripts/ops.py reset-health              — force-reset health checks
    python scripts/ops.py mark-stable               — mark current deploy as stable
    python scripts/ops.py rollback                  — rollback to last known good revision
    python scripts/ops.py settings                  — show runtime settings
    python scripts/ops.py settings --set KEY=VAL    — update a runtime setting
    python scripts/ops.py pool                      — show Max Plan account pool
    python scripts/ops.py accounts                  — manage accounts (--disable/--enable/--remove ID)
    python scripts/ops.py zones                     — show zone health
    python scripts/ops.py autonomy                  — show autonomous research state
    python scripts/ops.py diagnostics               — pipeline diagnostics (stuck tasks, circuit breakers)
    python scripts/ops.py alerts [--dismiss ID]     — show/dismiss system alerts
    python scripts/ops.py digest [--hours N]        — catch-up digest (default 24h)
    python scripts/ops.py activity [--limit N]      — recent activity feed
    python scripts/ops.py circuit-breaker [--reset PROJECT_ID] — circuit breaker state
    python scripts/ops.py requeue-failed            — requeue all failed tasks
    python scripts/ops.py restart-orchestrator      — restart background loops
    python scripts/ops.py message "text"            — send a message to the autonomous system
    python scripts/ops.py messages [--all]          — list open messages
"""
import json
import sys
from blueprint_api import api, load_config, list_tasks, all_tasks_by_status, fmt_task, get_project_id


def cmd_status(_args):
    """Full system overview: deploy state, pool, task counts."""
    # Deploy state
    try:
        ds = api("GET", "/api/admin/deploy-state")
        print("=== DEPLOY STATE ===")
        print(f"  Status:      {ds.get('status', '?')}")
        print(f"  Revision:    {ds.get('current_revision', '?')}")
        print(f"  Max VMs:     {ds.get('max_concurrent_vms', '?')}")
        print(f"  Failure rate:{ds.get('failure_rate', '?')}")
        fr = ds.get('rollback_count', 0)
        if fr:
            print(f"  Rollbacks:   {fr}")
        rr = ds.get('rollback_reason')
        if rr:
            print(f"  Rollback:    {rr}")
        pr = ds.get('pause_reason')
        if pr:
            print(f"  Pause reason:{pr}")
        eh = ds.get('external_health', {})
        if eh:
            errs = eh.get('errors', {})
            for dep in ['github_api', 'db', 'claude_api', 'gcp']:
                val = eh.get(dep)
                if val is not None:
                    status = 'OK' if val else 'DOWN'
                    err = errs.get(dep, '')
                    suffix = f" ({err})" if err else ""
                    print(f"  {dep:12}: {status}{suffix}")
    except Exception as e:
        print(f"Deploy state: error ({e})")

    # Settings
    print()
    try:
        s = api("GET", "/api/admin/settings")
        print("=== SETTINGS ===")
        for k in ['max_concurrent_vms', 'max_retries', 'max_autonomous_tasks_per_hour',
                   'autonomous_research_interval_minutes']:
            print(f"  {k}: {s.get(k, '?')}")
    except Exception as e:
        print(f"Settings: error ({e})")

    # Pool
    print()
    cmd_pool([])

    # Task counts
    print()
    print("=== TASK COUNTS ===")
    for status in ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED']:
        try:
            data = api("GET", f"/api/admin/tasks?limit=1&status={status}")
            print(f"  {status}: {data.get('total', '?')}")
        except:
            print(f"  {status}: ?")


def cmd_resume(_args):
    """Resume pipeline."""
    result = api("POST", "/api/admin/deploy-state/resume", {})
    print(f"Resumed: {json.dumps(result, indent=2)}")


def cmd_pause(_args):
    """Pause pipeline."""
    result = api("POST", "/api/admin/deploy-state/pause", {})
    print(f"Paused: {json.dumps(result, indent=2)}")


def cmd_repair(_args):
    """Repair stuck state."""
    result = api("POST", "/api/admin/repair-state", {})
    repairs = result.get('repairs', [])
    print(f"Repairs: {result.get('count', 0)}")
    for r in repairs:
        print(f"  - {r}")
    if not repairs:
        print("  (no repairs needed)")


def cmd_reset_health(_args):
    """Force-reset health checks."""
    result = api("POST", "/api/admin/deploy-state/reset-health", {})
    print(f"Health reset: {json.dumps(result, indent=2)}")


def cmd_mark_stable(_args):
    """Mark current deploy as stable."""
    result = api("POST", "/api/admin/deploy-state/mark-stable", {})
    print(f"Marked stable: {json.dumps(result, indent=2)}")


def cmd_rollback(_args):
    """Rollback to last known good revision. Pauses pipeline."""
    result = api("POST", "/api/admin/deploy-state/rollback", {})
    status = result.get('status', '?')
    print(f"Rollback: {status}")
    print(f"  From: {result.get('from_sha', '?')}")
    print(f"  To:   {result.get('to_sha', '?')} ({result.get('to_revision', '?')})")
    print(f"  Reason: {result.get('reason', '?')}")
    if result.get('gcloud_error'):
        print(f"  ERROR: {result['gcloud_error']}")
    if result.get('pipeline_paused'):
        print("  Pipeline paused — use 'resume' after verifying.")


def cmd_settings(args):
    """Show or update runtime settings."""
    if args and args[0] == '--set':
        updates = {}
        for kv in args[1:]:
            k, v = kv.split('=', 1)
            # Auto-convert types
            if v.lower() in ('true', 'false'):
                v = v.lower() == 'true'
            else:
                try:
                    v = int(v)
                except ValueError:
                    try:
                        v = float(v)
                    except ValueError:
                        pass
            updates[k] = v
        result = api("PATCH", "/api/admin/settings", updates)
        print(f"Updated: {json.dumps(result, indent=2)}")
    else:
        s = api("GET", "/api/admin/settings")
        print("=== RUNTIME SETTINGS ===")
        for k, v in sorted(s.items()):
            print(f"  {k}: {v}")


def cmd_pool(_args):
    """Show Max Plan account pool status with per-model rate-limit info."""
    try:
        s = api("GET", "/api/admin/settings")
        accounts = s.get('max_plan_accounts', [])
        print("=== MAX PLAN POOL ===")
        print(f"  Total:     {len(accounts)}")
        enabled_count = sum(1 for a in accounts if a.get('enabled', True))
        print(f"  Enabled:   {enabled_count}")
        if accounts:
            for a in accounts:
                name = a.get('name', a.get('id', '?')[:8])
                active = a.get('active_tasks', 0)
                enabled = a.get('enabled', True)
                status = 'OK' if enabled else 'DISABLED'
                rl = a.get('rl_hit_count', 0)
                suffix = f" (rate-limited x{rl})" if rl else ""
                print(f"  {name:20}: active={active} {status}{suffix}")
                dm = a.get('disabled_models', {})
                if dm:
                    for model, info in dm.items():
                        hits = info.get('hits', 0)
                        at = (info.get('at') or '')[:19]
                        print(f"    {model}: BLOCKED (hit #{hits}, since {at})")
    except Exception as e:
        print(f"Pool: error ({e})")


def _resolve_account(query):
    """Resolve an account by name (case-insensitive substring) or ID prefix."""
    s = api("GET", "/api/admin/settings")
    accounts = s.get('max_plan_accounts', [])
    q = query.lower()
    for a in accounts:
        if a['id'].lower().startswith(q) or q in a.get('name', '').lower():
            return a
    return None


def cmd_accounts(args):
    """Manage Max Plan accounts. Show pool with --disable/--enable/--remove for mutations.

    Account can be specified by full ID, ID prefix, or name (case-insensitive substring).
    Examples:
        python scripts/ops.py accounts --enable henry
        python scripts/ops.py accounts --enable all
        python scripts/ops.py accounts --disable evan
    """
    if args and args[0] in ('--disable', '--enable') and len(args) > 1:
        enable = args[0] == '--enable'
        target = args[1]
        if target.lower() == 'all':
            s = api("GET", "/api/admin/settings")
            accounts = s.get('max_plan_accounts', [])
            for a in accounts:
                if a.get('enabled', True) != enable:
                    result = api("PATCH", f"/api/admin/max-plan-accounts/{a['id']}", {"enabled": enable})
                    verb = "Enabled" if enable else "Disabled"
                    print(f"  {verb}: {result.get('name', a['id'][:8])}")
                else:
                    state = "enabled" if enable else "disabled"
                    print(f"  {a.get('name', a['id'][:8])}: already {state}")
            return
        acct = _resolve_account(target)
        if not acct:
            print(f"Account not found: {target}")
            return
        result = api("PATCH", f"/api/admin/max-plan-accounts/{acct['id']}", {"enabled": enable})
        verb = "Enabled" if enable else "Disabled"
        print(f"{verb}: {result.get('name', acct['id'][:8])}")
        return
    if args and args[0] == '--remove' and len(args) > 1:
        target = args[1]
        force = '--force' in args
        acct = _resolve_account(target)
        if not acct:
            print(f"Account not found: {target}")
            return
        path = f"/api/admin/max-plan-accounts/{acct['id']}"
        if force:
            path += "?force=true"
        result = api("DELETE", path)
        print(f"Removed: {result.get('message', 'done')}")
        return
    # Default: show accounts with full details
    cmd_pool([])


def cmd_zones(_args):
    """Show zone health."""
    zones = api("GET", "/api/admin/zone-health")
    print("=== ZONE HEALTH ===")
    if isinstance(zones, dict):
        for zone, data in sorted(zones.items()):
            status = data.get('status', '?')
            last = data.get('last_success', '?')
            fails = data.get('consecutive_failures', 0)
            print(f"  {zone:30}: {status:12} fails={fails} last_ok={last}")
    else:
        print(f"  {zones}")


def cmd_autonomy(_args):
    """Show autonomous research state per project."""
    try:
        data = api("GET", "/api/admin/autonomous-status")
        print("=== AUTONOMY STATE ===")
        projects = data.get('projects', [])
        if not projects:
            print("  No projects configured.")
            return
        for p in projects:
            print(f"  Project: {p.get('project_name', '?')}")
            print(f"    enabled:              {p.get('autonomous_enabled')}")
            print(f"    consecutive_failures: {p.get('consecutive_failures', 0)}")
            print(f"    tasks_last_hour:      {p.get('tasks_spawned_last_hour', 0)}")
            print(f"    last_research_at:     {p.get('last_research_at', 'never')}")
            direction = p.get('current_direction', {})
            if direction:
                print(f"    direction:            {(direction.get('summary') or '?')[:100]}")
    except Exception as e:
        print(f"Autonomy: error ({e})")


def cmd_diagnostics(_args):
    """Pipeline diagnostics: stuck tasks, circuit breakers, queue depth."""
    data = api("GET", "/api/admin/diagnostics")
    print("=== PIPELINE DIAGNOSTICS ===")
    print(f"  Queued:      {data.get('queued_count', '?')}")
    print(f"  In progress: {data.get('in_progress_count', '?')}")
    print(f"  Stuck:       {data.get('total_stuck_count', '?')}")

    stuck = data.get('stuck_tasks', [])
    if stuck:
        print("\n--- Stuck Tasks ---")
        for s in stuck:
            tid = s.get('task_id', '?')[:8] if s.get('task_id') else '?'
            title = (s.get('title') or '?')[:55]
            reason = s.get('reason', '?')
            print(f"  {tid}  {title:<55}  {reason}")

    projects = data.get('projects', [])
    if projects:
        print("\n--- Projects ---")
        for p in projects:
            pid = p.get('project_id', '?')[:8]
            cb = p.get('circuit_breaker_state', 'CLOSED')
            q = p.get('queued_count', 0)
            ip = p.get('in_progress_count', 0)
            stuck_ct = p.get('stuck_count', 0)
            print(f"  {pid}  queued={q}  running={ip}  stuck={stuck_ct}  breaker={cb}")

    cbs = data.get('circuit_breakers', {})
    if cbs:
        print("\n--- Circuit Breakers ---")
        for pid, cb in cbs.items():
            state = cb.get('state', '?')
            fails = cb.get('failure_count', 0)
            cooldown = cb.get('cooldown_ends_at', '')
            suffix = f"  cooldown_until={cooldown}" if cooldown else ""
            print(f"  {pid[:8]}  state={state}  failures={fails}{suffix}")

    zh = data.get('zone_health', {})
    if zh:
        avail = zh.get('available', '?')
        exhausted = zh.get('exhausted', [])
        print(f"\n  Zones: {avail} available" + (f", exhausted: {', '.join(exhausted)}" if exhausted else ""))


def cmd_alerts(args):
    """Show system alerts. Use --dismiss ID or --dismiss-all."""
    if args and args[0] == '--dismiss-all':
        result = api("POST", "/api/admin/alerts/dismiss-all", {})
        print(f"Dismissed: {result.get('dismissed_count', '?')} alerts")
        return
    if args and args[0] == '--dismiss' and len(args) > 1:
        result = api("POST", f"/api/admin/alerts/{args[1]}/dismiss", {})
        print(f"Dismissed: {result.get('status', 'ok')}")
        return

    alerts = api("GET", "/api/admin/alerts")
    if not alerts:
        print("No active alerts.")
        return
    print(f"=== ALERTS ({len(alerts)}) ===")
    for a in alerts:
        severity = a.get('severity', '?')
        title = a.get('title', '?')
        msg = (a.get('message') or '')[:120]
        ts = (a.get('created_at') or '')[:19]
        aid = a.get('id', '?')[:8]
        tid = (a.get('task_id') or '')[:8]
        task_ref = f"  task={tid}" if tid else ""
        print(f"  [{severity:8}] {aid}  {ts}  {title}{task_ref}")
        if msg:
            print(f"             {msg}")


def cmd_digest(args):
    """Catch-up digest — what happened in the last N hours."""
    hours = 24
    for i, a in enumerate(args):
        if a == '--hours' and i + 1 < len(args):
            hours = int(args[i + 1])
    data = api("GET", f"/api/admin/digest?hours={hours}")
    print(f"=== DIGEST (last {hours}h) ===")
    print(f"  Completed:        {data.get('completed_count', '?')}")
    print(f"  Failed:           {data.get('failed_count', '?')}")
    print(f"  PRs opened:       {data.get('prs_opened', '?')}")
    print(f"  Research cycles:  {data.get('research_cycles', '?')}")
    print(f"  Pending approval: {data.get('pending_approval_count', '?')}")
    print(f"  Active now:       {data.get('active_count', '?')} ({data.get('running_count', '?')} running, {data.get('queued_count', '?')} queued)")

    attention = data.get('needs_attention', [])
    if attention:
        print(f"\n--- Needs Attention ({len(attention)}) ---")
        for item in attention:
            reason = item.get('reason', '?')
            title = (item.get('title') or '?')[:55]
            wait_h = item.get('waiting_hours')
            tid = (item.get('task_id') or '?')[:8]
            wait_str = f"{wait_h}h ago" if wait_h is not None else "?"
            print(f"  {tid}  [{reason:18}]  {title}  ({wait_str})")
            err = item.get('error_snippet')
            if err:
                print(f"           {err[:120]}")

    completions = data.get('recent_completions', [])
    if completions:
        print(f"\n--- Recent Completions ---")
        for c in completions:
            tid = (c.get('task_id') or '?')[:8]
            title = (c.get('title') or '?')[:50]
            ttype = c.get('task_type', '?')
            pr = c.get('pr_url', '')
            ts = (c.get('completed_at') or '')[:16]
            pr_str = f"  {pr}" if pr else ""
            print(f"  {tid}  [{ttype:8}]  {ts}  {title}{pr_str}")

    failures = data.get('recent_failures', [])
    if failures:
        print(f"\n--- Recent Failures ---")
        for f in failures:
            tid = (f.get('task_id') or '?')[:8]
            title = (f.get('title') or '?')[:50]
            ts = (f.get('failed_at') or '')[:16]
            err = (f.get('error_snippet') or '')[:100]
            print(f"  {tid}  {ts}  {title}")
            if err:
                print(f"           {err}")


def cmd_activity(args):
    """Recent activity feed — completions, failures, research findings."""
    limit = 20
    for i, a in enumerate(args):
        if a == '--limit' and i + 1 < len(args):
            limit = int(args[i + 1])
    events = api("GET", f"/api/admin/activity?limit={limit}")
    if not events:
        print("No recent activity.")
        return
    icons = {
        'TASK_COMPLETED': 'done', 'TASK_FAILED': 'FAIL',
        'RESEARCH_FINDINGS': 'research', 'DEPLOY': 'deploy',
    }
    print(f"=== ACTIVITY (last {len(events)} events) ===")
    for e in events:
        etype = e.get('type', '?')
        icon = icons.get(etype, etype)
        ts = (e.get('timestamp') or '')[:16]
        title = (e.get('title') or '?')[:55]
        tid = (e.get('task_id') or '')[:8]
        pr = e.get('pr_url') or ''
        err = (e.get('error_summary') or '')[:80]
        dur = e.get('duration_seconds')
        dur_str = f"  {int(dur)}s" if dur else ""
        pr_str = f"  {pr}" if pr else ""
        print(f"  {ts}  [{icon:10}]  {tid}  {title}{dur_str}{pr_str}")
        if err:
            print(f"                              {err}")


def cmd_circuit_breaker(args):
    """Show circuit breaker state, optionally reset for a project."""
    if args and args[0] == '--reset':
        pid = args[1] if len(args) > 1 else get_project_id()
        result = api("POST", f"/api/admin/projects/{pid}/circuit-breaker/reset", {})
        print(f"Circuit breaker reset: {json.dumps(result, indent=2)}")
        return
    data = api("GET", "/api/admin/diagnostics")
    cbs = data.get('circuit_breakers', {})
    if not cbs:
        print("No circuit breakers registered.")
        return
    print("=== CIRCUIT BREAKERS ===")
    for pid, cb in cbs.items():
        state = cb.get('state', '?')
        fails = cb.get('failure_count', 0)
        successes = cb.get('success_count', 0)
        opened = cb.get('opened_at', '')
        cooldown = cb.get('cooldown_ends_at', '')
        print(f"  {pid[:8]}  state={state}  failures={fails}  successes={successes}")
        if opened:
            print(f"           opened_at={opened}  cooldown_until={cooldown or '?'}")


def cmd_requeue_failed(_args):
    """Requeue all FAILED tasks."""
    failed = list_tasks(status="FAILED", limit=100)
    if not failed:
        print("No failed tasks.")
        return
    print(f"Requeuing {len(failed)} failed tasks...")
    for t in failed:
        tid = t['id']
        try:
            api("PATCH", f"/api/admin/tasks/{tid}", {"status": "QUEUED"})
            print(f"  {tid[:8]} requeued: {(t.get('title','') or '')[:50]}")
        except Exception as e:
            print(f"  {tid[:8]} FAILED: {e}")


def cmd_restart_orchestrator(_args):
    """Restart background loops."""
    result = api("POST", "/api/admin/restart-orchestrator", {})
    print(f"Restart: {json.dumps(result, indent=2)}")


def cmd_message(args):
    """Send a message to the autonomous system.

    Usage: ops.py message "your message here" [--type NOTE|FEEDBACK|BUG_REPORT|QUESTION|BLOCKER]
    """
    if not args:
        print("Usage: ops.py message \"your message\" [--type NOTE]")
        return
    msg_type = "FEEDBACK"
    content_parts = []
    i = 0
    while i < len(args):
        if args[i] == '--type' and i + 1 < len(args):
            msg_type = args[i + 1]
            i += 2
        else:
            content_parts.append(args[i])
            i += 1
    content = ' '.join(content_parts)
    if not content:
        print("Error: message content required")
        return
    pid = get_project_id()
    result = api("POST", f"/api/projects/{pid}/messages", {
        "content": content,
        "message_type": msg_type,
    })
    print(f"Message sent: {result.get('id', '?')}")


def cmd_messages(args):
    """List open messages."""
    pid = get_project_id()
    status = "open"
    if args and args[0] == '--all':
        status = ""
    url = f"/api/projects/{pid}/messages"
    if status:
        url += f"?status={status}"
    msgs = api("GET", url)
    if not msgs:
        print("No messages.")
        return
    for m in msgs:
        icon = {"BUG_REPORT": "BUG", "FEEDBACK": "FB", "NOTE": "NOTE", "QUESTION": "Q", "BLOCKER": "BLOCK"}.get(m.get("type", ""), "?")
        src = m.get("from", "?")
        age = m.get("created_at", "?")[:16] if m.get("created_at") else "?"
        print(f"  [{icon}] ({src}, {age}) {m['content'][:120]}")


COMMANDS = {
    'status': cmd_status,
    'resume': cmd_resume,
    'pause': cmd_pause,
    'repair': cmd_repair,
    'reset-health': cmd_reset_health,
    'mark-stable': cmd_mark_stable,
    'rollback': cmd_rollback,
    'settings': cmd_settings,
    'pool': cmd_pool,
    'accounts': cmd_accounts,
    'zones': cmd_zones,
    'autonomy': cmd_autonomy,
    'diagnostics': cmd_diagnostics,
    'alerts': cmd_alerts,
    'digest': cmd_digest,
    'activity': cmd_activity,
    'circuit-breaker': cmd_circuit_breaker,
    'requeue-failed': cmd_requeue_failed,
    'restart-orchestrator': cmd_restart_orchestrator,
    'message': cmd_message,
    'messages': cmd_messages,
}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:", ', '.join(sorted(COMMANDS)))
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
