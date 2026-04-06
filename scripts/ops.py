"""Blueprint operations CLI — deploy state, pool health, settings, and system ops.

Usage:
    python scripts/ops.py status                    — full system overview
    python scripts/ops.py resume                    — resume pipeline (unpause)
    python scripts/ops.py pause                     — pause pipeline
    python scripts/ops.py repair                    — repair stuck tasks, orphaned parents
    python scripts/ops.py reset-health              — force-reset health checks
    python scripts/ops.py mark-stable               — mark current deploy as stable
    python scripts/ops.py settings                  — show runtime settings
    python scripts/ops.py settings --set KEY=VAL    — update a runtime setting
    python scripts/ops.py pool                      — show Max Plan account pool
    python scripts/ops.py zones                     — show zone health
    python scripts/ops.py requeue-failed            — requeue all failed tasks
    python scripts/ops.py restart-orchestrator      — restart background loops
"""
import json
import sys
from blueprint_api import api, load_config, list_tasks, all_tasks_by_status, fmt_task


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
    """Show Max Plan account pool status."""
    try:
        h = api("GET", "/api/health/detailed")
        pool = h.get('components', {}).get('max_plan_pool', {})
        print("=== MAX PLAN POOL ===")
        print(f"  Total:     {pool.get('total', '?')}")
        print(f"  Available: {pool.get('available', '?')}")
        accounts = pool.get('accounts', [])
        if accounts:
            for a in accounts:
                name = a.get('name', a.get('id', '?')[:8])
                active = a.get('active_tasks', 0)
                enabled = a.get('enabled', True)
                status = 'OK' if enabled else 'DISABLED'
                rl = a.get('rl_hit_count', 0)
                suffix = f" (rate-limited x{rl})" if rl else ""
                print(f"  {name:20}: active={active} {status}{suffix}")
    except Exception as e:
        print(f"Pool: error ({e})")


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


COMMANDS = {
    'status': cmd_status,
    'resume': cmd_resume,
    'pause': cmd_pause,
    'repair': cmd_repair,
    'reset-health': cmd_reset_health,
    'mark-stable': cmd_mark_stable,
    'settings': cmd_settings,
    'pool': cmd_pool,
    'zones': cmd_zones,
    'requeue-failed': cmd_requeue_failed,
    'restart-orchestrator': cmd_restart_orchestrator,
}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:", ', '.join(sorted(COMMANDS)))
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
