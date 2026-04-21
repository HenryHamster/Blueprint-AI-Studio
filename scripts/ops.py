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
    python scripts/ops.py directions                — show project directions, notes, and history
    python scripts/ops.py vision                    — show product vision and design document
    python scripts/ops.py health                    — deploy health deep-dive (probe ring buffer, cycle age)
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
            for dep in ['github_api', 'db', 'dispatch_api', 'anthropic_api', 'openai_api', 'gemini_api', 'gcp']:
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


def _age_str(iso_or_dt):
    """Return 'Ns ago' / 'Nm ago' for an ISO string or None."""
    if not iso_or_dt:
        return 'never'
    from datetime import datetime, timezone
    try:
        if isinstance(iso_or_dt, str):
            s = iso_or_dt.replace('Z', '+00:00')
            ts = datetime.fromisoformat(s)
        else:
            ts = iso_or_dt
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return str(iso_or_dt)
    if delta < 60:
        return f'{int(delta)}s ago'
    if delta < 3600:
        return f'{int(delta // 60)}m{int(delta % 60)}s ago'
    return f'{int(delta // 3600)}h{int((delta % 3600) // 60)}m ago'


def cmd_health(_args):
    """Deep-dive on deploy health: probe ring buffer, cycle age, failure streaks."""
    try:
        ds = api("GET", "/api/admin/deploy-state")
    except Exception as e:
        print(f"Deploy state: error ({e})")
        return

    print("=== DEPLOY HEALTH ===")
    print(f"  Status:       {ds.get('status', '?')}")
    pr = ds.get('pause_reason')
    if pr:
        print(f"  Pause reason: {pr}")
    paused_at = ds.get('paused_at')
    if paused_at:
        print(f"  Paused:       {_age_str(paused_at)}")

    eh = ds.get('external_health') or {}
    errs = eh.get('errors') or {}
    print()
    print("=== EXTERNAL DEPS ===")
    for dep in ('github_api', 'db', 'dispatch_api', 'anthropic_api', 'openai_api', 'gemini_api', 'gcp'):
        val = eh.get(dep)
        if val is None:
            continue
        status = 'OK' if val else 'DOWN'
        err = errs.get(dep, '')
        suffix = f" — {err}" if err else ''
        print(f"  {dep:12}: {status}{suffix}")
    consec = eh.get('consecutive_failures') or {}
    if any(consec.values()):
        print(f"  consecutive failures: {consec}")

    print()
    print("=== PROBE SCHEDULE ===")
    interval = ds.get('health_check_interval_seconds')
    last = eh.get('last_check')
    print(f"  Interval:     {interval}s" + (" (paused)" if interval == 15 else ""))
    print(f"  Last probe:   {_age_str(last)}")
    if last and interval:
        from datetime import datetime, timezone
        try:
            ts = datetime.fromisoformat(last.replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            next_in = interval - (datetime.now(timezone.utc) - ts).total_seconds()
            if next_in <= 0:
                print(f"  Next probe:   running / overdue by {int(-next_in)}s")
            else:
                print(f"  Next probe:   in {int(next_in)}s")
        except Exception:
            pass
    elif last is None:
        print("  Next probe:   unknown — last_check is None (orchestrator may not have run _check_external_health yet)")

    print()
    print("=== RING BUFFER (3-of-5 gate) ===")
    hist = ds.get('health_recovery_history') or []
    size = ds.get('health_recovery_history_size') or 5
    required = ds.get('health_recovery_streak_required') or 3
    streak = ds.get('health_recovery_streak') or 0
    dots = []
    for i in range(size):
        if i < len(hist):
            dots.append('OK ' if hist[i] else 'FAIL')
        else:
            dots.append('--  ')
    print(f"  Recovery:     [{' | '.join(dots)}]  {streak}/{required} healthy")
    fs = ds.get('health_failure_streak') or 0
    ft = ds.get('health_failure_streak_threshold') or 3
    if fs:
        print(f"  Failure:      {fs}/{ft} consecutive failures before pause")

    print()
    print("=== ORCHESTRATOR CYCLE ===")
    try:
        hd = api("GET", "/api/health/detailed")
        comp = (hd.get('components') or {})
        orch = comp.get('orchestrator') or {}
        status = orch.get('status', '?')
        cycle_age = orch.get('cycle_age_seconds')
        print(f"  Status:       {status}")
        if cycle_age is None:
            print("  Last cycle:   never (loop has not completed a cycle since process start)")
        else:
            print(f"  Last cycle:   {int(cycle_age)}s ago")
            if cycle_age > 30:
                print("  WARNING: cycle >30s old — loop may be stuck")
        if status == 'down':
            print("  ACTION: try 'python scripts/ops.py restart-orchestrator'")
        print(f"  Queued:       {orch.get('queued_tasks', '?')}")
        print(f"  Active:       {orch.get('active_tasks', '?')}")
    except Exception as e:
        print(f"  (health/detailed unavailable: {e})")


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


def cmd_directions(args):
    """Show project directions with status, notes, and research history.

    Usage:
        ops.py directions                   — show all active/proposed directions
        ops.py directions --all             — include paused/completed/expired
        ops.py directions --id d-xxxx       — show single direction in detail
    """
    pid = get_project_id()
    data = api("GET", f"/api/admin/projects/{pid}/directions")
    directions = data.get('directions', [])

    if not directions:
        print("No directions configured.")
        return

    # Single direction detail view
    if args and args[0] == '--id' and len(args) > 1:
        target = args[1]
        match = [d for d in directions if d.get('id', '').startswith(target)]
        if not match:
            print(f"Direction not found: {target}")
            return
        d = match[0]
        print(f"=== DIRECTION: {d.get('id', '?')} ===")
        print(f"  Text:       {d.get('text', '?')}")
        print(f"  Status:     {d.get('status', '?')}")
        print(f"  Priority:   {d.get('priority', '?')}")
        print(f"  Category:   {d.get('category', '-')}")
        print(f"  Merge rate: {d.get('merge_rate', 0):.2f}")
        print(f"  Added:      {(d.get('added_at') or '?')[:19]}")
        if d.get('paths'):
            print(f"  Paths:      {', '.join(d['paths'])}")
        if d.get('completion_criteria'):
            print(f"  Criteria:   {d['completion_criteria']}")
        if d.get('possibly_complete'):
            print(f"  ** Possibly complete: {d.get('completion_suggestion', '(no suggestion)')}")
        if d.get('possibly_saturated'):
            print(f"  ** Possibly saturated")
        if d.get('conflicts_with'):
            print(f"  Conflicts:  {', '.join(d['conflicts_with'])}")
        if d.get('pending_update'):
            pu = d['pending_update']
            print(f"\n  --- Pending Update ---")
            if pu.get('suggested_text'):
                print(f"    Text:     {pu['suggested_text'][:150]}")
            if pu.get('suggested_priority'):
                print(f"    Priority: {pu['suggested_priority']}")
            if pu.get('rationale'):
                print(f"    Reason:   {pu['rationale'][:200]}")

        notes = d.get('notes', [])
        if notes:
            print(f"\n  --- Notes ({len(notes)}) ---")
            for n in notes:
                ts = (n.get('added_at') or n.get('timestamp') or '?')[:16]
                text = n.get('text', '?')
                print(f"    [{ts}] {text[:200]}")

        history = d.get('research_history', [])
        agg = d.get('history_aggregate', {})
        if history or agg:
            total_cycles = len(history) + (agg.get('total_cycles') or 0)
            total_merged = sum(e.get('prs_merged', 0) for e in history) + (agg.get('total_prs_merged') or 0)
            total_tasks = sum(e.get('tasks_created', 0) for e in history) + (agg.get('total_tasks_created') or 0)
            print(f"\n  --- Research History ({total_cycles} cycles, {total_tasks} tasks, {total_merged} merged) ---")
            for i, h in enumerate(history[-5:]):  # last 5 cycles
                ts = (h.get('researched_at') or '?')[:16]
                tc = h.get('tasks_created', 0)
                pm = h.get('prs_merged', 0)
                print(f"    {ts}  tasks={tc}  merged={pm}")

        ctx = d.get('research_context', '')
        if ctx:
            print(f"\n  --- Research Context ---")
            for line in ctx.strip().split('\n')[:20]:
                print(f"    {line}")
            lines = ctx.strip().split('\n')
            if len(lines) > 20:
                print(f"    ... ({len(lines) - 20} more lines)")
        return

    # List view
    show_all = args and args[0] == '--all'
    visible_statuses = {'active', 'proposed'} if not show_all else None

    # Group by status
    grouped: dict[str, list] = {}
    for d in directions:
        status = d.get('status', 'active')
        if visible_statuses and status not in visible_statuses:
            continue
        grouped.setdefault(status, []).append(d)

    status_order = ['proposed', 'active', 'paused', 'completed', 'expired']
    status_icons = {'proposed': '?', 'active': '*', 'paused': '|', 'completed': '+', 'expired': 'x'}

    print(f"=== DIRECTIONS ({sum(len(v) for v in grouped.values())} shown) ===")
    for status in status_order:
        dirs_in_status = grouped.get(status, [])
        if not dirs_in_status:
            continue
        print(f"\n  [{status.upper()}]")
        for d in dirs_in_status:
            did = d.get('id', '?')
            pri = d.get('priority', 'med')[:3]
            cat = (d.get('category') or '-')[:5]
            text = d.get('text', '?')[:80]
            mr = d.get('merge_rate', 0)
            icon = status_icons.get(status, ' ')
            notes_ct = len(d.get('notes', []))
            notes_str = f"  [{notes_ct} notes]" if notes_ct else ""
            flags = []
            if d.get('possibly_complete'):
                flags.append('COMPLETE?')
            if d.get('possibly_saturated'):
                flags.append('SATURATED?')
            if d.get('pending_update'):
                flags.append('UPDATE?')
            flag_str = f"  !! {', '.join(flags)}" if flags else ""
            print(f"    {icon} {did}  [{pri:3}] [{cat:5}]  mr={mr:.1f}  {text}{notes_str}{flag_str}")

    if not show_all:
        hidden = len(directions) - sum(len(v) for v in grouped.values())
        if hidden > 0:
            print(f"\n  ({hidden} paused/completed/expired hidden — use --all to show)")


def cmd_vision(args):
    """Show the product vision and design document.

    Usage:
        ops.py vision                       — show the strategic vision
        ops.py vision --design-doc          — show the design document (architecture wiki)
        ops.py vision --design-doc KEY      — show a single design doc page
    """
    import io, os
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    pid = get_project_id()
    data = api("GET", f"/api/admin/projects/{pid}/directions")

    if args and args[0] == '--design-doc':
        doc = data.get('design_doc', {})
        if not doc:
            print("No design document yet.")
            return
        if len(args) > 1:
            key = args[1]
            page = doc.get(key)
            if page is None:
                print(f"Page not found: {key}")
                print(f"Available pages: {', '.join(sorted(doc.keys()))}")
                return
            print(f"=== DESIGN DOC: {key} ===")
            print(json.dumps(page, indent=2))
        else:
            print(f"=== DESIGN DOCUMENT ({len(doc)} pages) ===")
            for key in sorted(doc.keys()):
                page = doc[key]
                if isinstance(page, dict):
                    summary = page.get('summary', page.get('description', str(page)[:120]))
                    status = page.get('status', '')
                    status_str = f" [{status}]" if status else ''
                    print(f"  {key}{status_str} — {str(summary)[:100]}")
                else:
                    print(f"  {key} — {str(page)[:100]}")
        return

    vision = data.get('vision', {})
    if not vision:
        print("No vision set yet.")
        return

    print("=== PRODUCT VISION ===")
    print(f"  Purpose:  {vision.get('purpose', '(not set)')}")
    focus = vision.get('focus')
    if focus:
        if isinstance(focus, list):
            print(f"  Focus ({len(focus)} active):")
            for f in focus:
                print(f"    - {f}")
        else:
            print(f"  Focus:    {focus}")
    print(f"  Revision: {vision.get('revision', '?')}")
    print(f"  Updated:  {(vision.get('last_updated') or '?')[:19]}")
    criteria = vision.get('success_criteria', [])
    if criteria:
        print(f"\n  Success criteria ({len(criteria)}):")
        for c in criteria:
            print(f"    - {c}")
    systems = vision.get('systems', [])
    if systems:
        print(f"\n  Systems ({len(systems)}):")
        for s in systems:
            sid = s.get('id', '?')
            name = s.get('name', '?')
            status = s.get('status', '?')
            print(f"    {sid} [{status}] {name}")
    context = vision.get('context', '')
    if context:
        print(f"\n  Context:\n    {context[:500]}")
    cycles = vision.get('_strategic_cycle_count', 0)
    spawns = vision.get('_research_spawn_count', 0)
    if cycles or spawns:
        print(f"\n  Stats: {spawns} research spawns, {cycles} strategic cycles")


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
    'directions': cmd_directions,
    'vision': cmd_vision,
    'health': cmd_health,
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
