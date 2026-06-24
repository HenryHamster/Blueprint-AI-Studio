"""Blueprint operations CLI — pool health, settings, and system ops.

Several subcommands have been superseded by ``python scripts/blueprint.py``
and are now deprecated stubs that print a pointer and exit non-zero:

    DEPRECATED — use blueprint.py instead:
        status       → python scripts/blueprint.py status
        qualify      → python scripts/blueprint.py promote-recover qualify
        rollback     → python scripts/blueprint.py promote-recover rollback
        mark-stable  → python scripts/blueprint.py promote-recover mark-stable
        resume       → python scripts/blueprint.py promote-recover resume
        pause        → python scripts/blueprint.py promote-recover <action>

    DEPRECATED — use blueprint.py fleet instead:
        pool         → python scripts/blueprint.py fleet pool
        warm         → python scripts/blueprint.py fleet warm
        accounts     → python scripts/blueprint.py fleet accounts [--disable/--enable/--remove NAME] [--yes]
        zones        → python scripts/blueprint.py fleet zones

    DEPRECATED — use blueprint.py ops instead:
        repair                    → python scripts/blueprint.py ops repair
        reset-health              → python scripts/blueprint.py ops reset-health
        requeue-failed            → python scripts/blueprint.py ops requeue-failed
        restart-orchestrator      → python scripts/blueprint.py ops restart-orchestrator
        circuit-breaker           → python scripts/blueprint.py ops circuit-breaker [--reset PROJECT_ID]

Active subcommands (not covered by blueprint.py):
    python scripts/ops.py autonomy                  — show autonomous research state
    python scripts/ops.py directions                — show project directions, notes, and history
    python scripts/ops.py vision                    — show product vision and design document
    python scripts/ops.py health                    — deploy health deep-dive (probe ring buffer, cycle age)
    python scripts/ops.py diagnostics               — pipeline diagnostics (stuck tasks, circuit breakers)
    python scripts/ops.py alerts [--dismiss ID]     — show/dismiss system alerts
    python scripts/ops.py digest [--hours N]        — catch-up digest (default 24h)
    python scripts/ops.py activity [--limit N]      — recent activity feed
    python scripts/ops.py message "text"            — send a message to the autonomous system
    python scripts/ops.py messages [--all]          — list open messages
    python scripts/ops.py fingerprints              — list open residual remediation cases
    python scripts/ops.py fingerprints --all        — every status
    python scripts/ops.py fingerprints --ignore <fp-substring>
    python scripts/ops.py fingerprints --unignore <fp-substring>
    python scripts/ops.py fingerprints --clear      — wipe in-memory residual store
    python scripts/ops.py regression-cases          — list ACTIVE/REMEDIATING/NMI cases
    python scripts/ops.py regression-cases --close <class> [--reason "..."]
    python scripts/ops.py regression-cases --close-prefix <prefix> [--reason "..."]
"""
import json
import sys
from blueprint_api import api, load_config, list_tasks, all_tasks_by_status, fmt_task, get_project_id


def cmd_status(_args):
    """DEPRECATED — use: python scripts/blueprint.py status"""
    print("DEPRECATED: use python scripts/blueprint.py status")
    sys.exit(1)


def cmd_resume(_args):
    """DEPRECATED — use: python scripts/blueprint.py promote-recover resume"""
    print("DEPRECATED: use python scripts/blueprint.py promote-recover resume")
    sys.exit(1)


def cmd_pause(_args):
    """DEPRECATED — use: python scripts/blueprint.py promote-recover <action>"""
    print(
        "DEPRECATED: scripts/ops.py pause is superseded.\n"
        "Use: python scripts/blueprint.py promote-recover <action>\n"
        "Available actions: arm, hold, clear-override, cut, qualify, "
        "clear-deploy-failed, rollback, mark-stable, resume"
    )
    sys.exit(1)


def cmd_repair(_args):
    """DEPRECATED — use: python scripts/blueprint.py ops repair"""
    print("DEPRECATED: use python scripts/blueprint.py ops repair")
    sys.exit(1)


def cmd_reset_health(_args):
    """DEPRECATED — use: python scripts/blueprint.py ops reset-health"""
    print("DEPRECATED: use python scripts/blueprint.py ops reset-health")
    sys.exit(1)


def cmd_mark_stable(_args):
    """DEPRECATED — use: python scripts/blueprint.py promote-recover mark-stable"""
    print("DEPRECATED: use python scripts/blueprint.py promote-recover mark-stable")
    sys.exit(1)


def cmd_rollback(_args):
    """DEPRECATED — use: python scripts/blueprint.py promote-recover rollback"""
    print("DEPRECATED: use python scripts/blueprint.py promote-recover rollback")
    sys.exit(1)


def cmd_settings(_args):
    """DEPRECATED — use: python scripts/blueprint.py settings"""
    print(
        "DEPRECATED: use python scripts/blueprint.py settings\n"
        "  show:          python scripts/blueprint.py settings show\n"
        "  set KEY=VAL:   python scripts/blueprint.py settings set KEY=VAL --yes"
    )
    sys.exit(1)


def cmd_pool(_args):
    """DEPRECATED — use: python scripts/blueprint.py fleet pool"""
    print("DEPRECATED: use python scripts/blueprint.py fleet pool")
    sys.exit(1)


def cmd_warm(_args):
    """DEPRECATED — use: python scripts/blueprint.py fleet warm"""
    print("DEPRECATED: use python scripts/blueprint.py fleet warm")
    sys.exit(1)


def cmd_accounts(_args):
    """DEPRECATED — use: python scripts/blueprint.py fleet accounts"""
    print(
        "DEPRECATED: use python scripts/blueprint.py fleet accounts\n"
        "  show:          python scripts/blueprint.py fleet accounts\n"
        "  disable:       python scripts/blueprint.py fleet accounts --disable NAME --yes\n"
        "  enable:        python scripts/blueprint.py fleet accounts --enable NAME --yes\n"
        "  remove:        python scripts/blueprint.py fleet accounts --remove NAME --yes"
    )
    sys.exit(1)


def cmd_zones(_args):
    """DEPRECATED — use: python scripts/blueprint.py fleet zones"""
    print("DEPRECATED: use python scripts/blueprint.py fleet zones")
    sys.exit(1)


def cmd_autonomy(_args):
    """DEPRECATED — use: python scripts/blueprint.py health autonomy"""
    print("DEPRECATED: use python scripts/blueprint.py health autonomy")
    sys.exit(1)


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
    """DEPRECATED — use: python scripts/blueprint.py health deploy"""
    print("DEPRECATED: use python scripts/blueprint.py health deploy")
    sys.exit(1)


def cmd_diagnostics(_args):
    """DEPRECATED — use: python scripts/blueprint.py diagnostics"""
    print("DEPRECATED: use python scripts/blueprint.py diagnostics")
    sys.exit(1)


def cmd_alerts(_args):
    """DEPRECATED — use: python scripts/blueprint.py health alerts"""
    print(
        "DEPRECATED: use python scripts/blueprint.py health alerts\n"
        "  dismiss one:  python scripts/blueprint.py health alerts --dismiss <id>\n"
        "  dismiss all:  python scripts/blueprint.py health alerts --dismiss-all"
    )
    sys.exit(1)


def cmd_digest(_args):
    """DEPRECATED — use: python scripts/blueprint.py health digest"""
    print(
        "DEPRECATED: use python scripts/blueprint.py health digest\n"
        "  with hours:  python scripts/blueprint.py health digest --hours 48"
    )
    sys.exit(1)


def cmd_activity(_args):
    """DEPRECATED — use: python scripts/blueprint.py health activity"""
    print(
        "DEPRECATED: use python scripts/blueprint.py health activity\n"
        "  with limit:  python scripts/blueprint.py health activity --limit 50"
    )
    sys.exit(1)


def cmd_circuit_breaker(_args):
    """DEPRECATED — use: python scripts/blueprint.py ops circuit-breaker"""
    print(
        "DEPRECATED: use python scripts/blueprint.py ops circuit-breaker\n"
        "  show:  python scripts/blueprint.py ops circuit-breaker\n"
        "  reset: python scripts/blueprint.py ops circuit-breaker --reset PROJECT_ID"
    )
    sys.exit(1)


def cmd_requeue_failed(_args):
    """DEPRECATED — use: python scripts/blueprint.py ops requeue-failed"""
    print("DEPRECATED: use python scripts/blueprint.py ops requeue-failed")
    sys.exit(1)


def cmd_restart_orchestrator(_args):
    """DEPRECATED — use: python scripts/blueprint.py ops restart-orchestrator"""
    print("DEPRECATED: use python scripts/blueprint.py ops restart-orchestrator")
    sys.exit(1)


def cmd_message(_args):
    """DEPRECATED — use: python scripts/blueprint.py messages send"""
    print('DEPRECATED: use python scripts/blueprint.py messages send "your message" [--type TYPE]')
    sys.exit(1)


def cmd_messages(_args):
    """DEPRECATED — use: python scripts/blueprint.py messages list"""
    print("DEPRECATED: use python scripts/blueprint.py messages list [--status open]")
    sys.exit(1)


def cmd_directions(args):
    """DEPRECATED — use: python scripts/blueprint.py directions"""
    print("DEPRECATED: use python scripts/blueprint.py directions [list|show|add|update|unhold]")
    import sys; sys.exit(1)


def cmd_vision(args):
    """DEPRECATED — use: python scripts/blueprint.py vision"""
    print(
        "DEPRECATED: use python scripts/blueprint.py vision\n"
        "  Show vision:      python scripts/blueprint.py vision\n"
        "  Show design doc:  python scripts/blueprint.py vision --design-doc\n"
        "  Show page KEY:    python scripts/blueprint.py vision --design-doc KEY"
    )
    sys.exit(1)


def cmd_strategic_cycle(args):
    """DEPRECATED — use: python scripts/blueprint.py strategic-cycle"""
    print(
        "DEPRECATED: use python scripts/blueprint.py strategic-cycle\n"
        "  Full cycle:     python scripts/blueprint.py strategic-cycle --yes\n"
        "  Phase 2 only:   python scripts/blueprint.py strategic-cycle --phase strategic_review --yes"
    )
    sys.exit(1)


def cmd_trim_playbook_archive(args):
    """DEPRECATED — use: python scripts/blueprint.py trim-playbook-archive"""
    print(
        "DEPRECATED: use python scripts/blueprint.py trim-playbook-archive\n"
        "  Trim to 50:   python scripts/blueprint.py trim-playbook-archive\n"
        "  Trim to N:    python scripts/blueprint.py trim-playbook-archive --keep N\n"
        "  Dry run:      python scripts/blueprint.py trim-playbook-archive --dry-run"
    )
    sys.exit(1)


def cmd_fingerprints(args):
    """DEPRECATED — use: python scripts/blueprint.py fingerprints"""
    print(
        "DEPRECATED: use python scripts/blueprint.py fingerprints\n"
        "  List:     python scripts/blueprint.py fingerprints\n"
        "  All:      python scripts/blueprint.py fingerprints --all\n"
        "  Ignore:   python scripts/blueprint.py fingerprints --ignore <fp-substring>\n"
        "  Unignore: python scripts/blueprint.py fingerprints --unignore <fp-substring>\n"
        "  Clear:    python scripts/blueprint.py fingerprints --clear"
    )
    sys.exit(1)


def cmd_regression_cases(args):
    """DEPRECATED — use: python scripts/blueprint.py regression-cases"""
    print(
        "DEPRECATED: use python scripts/blueprint.py regression-cases\n"
        "  List:         python scripts/blueprint.py regression-cases\n"
        "  All:          python scripts/blueprint.py regression-cases --all\n"
        "  Close:        python scripts/blueprint.py regression-cases --close CLASS [--reason 'why']\n"
        "  Close prefix: python scripts/blueprint.py regression-cases --close-prefix PREFIX"
    )
    sys.exit(1)


def cmd_qualify(args):
    """DEPRECATED — use: python scripts/blueprint.py promote-recover qualify"""
    print("DEPRECATED: use python scripts/blueprint.py promote-recover qualify")
    sys.exit(1)


COMMANDS = {
    'status': cmd_status,
    'qualify': cmd_qualify,
    'resume': cmd_resume,
    'pause': cmd_pause,
    'repair': cmd_repair,
    'reset-health': cmd_reset_health,
    'mark-stable': cmd_mark_stable,
    'rollback': cmd_rollback,
    'settings': cmd_settings,
    'pool': cmd_pool,
    'warm': cmd_warm,
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
    'trim-playbook-archive': cmd_trim_playbook_archive,
    'strategic-cycle': cmd_strategic_cycle,
    'fingerprints': cmd_fingerprints,
    'regression-cases': cmd_regression_cases,
}

if __name__ == '__main__':
    # Windows consoles default to cp1252; force UTF-8 so non-ASCII glyphs in
    # status output (arrows, em-dashes) don't crash with UnicodeEncodeError —
    # e.g. the "Closed: ... -> ..." line in cmd_regression_cases, which
    # previously made a successful close print a misleading "Error closing".
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:", ', '.join(sorted(COMMANDS)))
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
