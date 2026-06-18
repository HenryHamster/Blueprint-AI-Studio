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


def cmd_strategic_cycle(args):
    """Manually trigger a strategic cycle.

    Usage:
        ops.py strategic-cycle                  -- full cycle (design_doc -> chains to strategic_review)
        ops.py strategic-cycle --phase strategic_review  -- Phase 2 only (no design doc update)
    """
    phase = "design_doc"
    i = 0
    while i < len(args):
        if args[i] == '--phase' and i + 1 < len(args):
            phase = args[i + 1]; i += 2
        else:
            i += 1
    pid = get_project_id()
    result = api("POST", f"/api/admin/projects/{pid}/strategic-cycle?phase={phase}")
    task_id = result.get("task_id") or result.get("id") or "?"
    print(f"Spawned {phase} task: {task_id}")
    print(f"  Title: {result.get('title', '?')}")
    if phase == "design_doc":
        print("  Phase 1 will chain into strategic_review (Phase 2) on completion.")


def cmd_trim_playbook_archive(args):
    """Trim design_doc.playbook_archive to the last N entries (default 50).

    One-shot cleanup for projects that accumulated archive entries before the
    mechanical cap shipped. The live code caps archive length on every
    _archive_least_useful_entry call, but that only kicks in on the next
    archival event — this forces an immediate trim.

    Usage:
        ops.py trim-playbook-archive              — trim to 50 entries
        ops.py trim-playbook-archive --keep 20    — trim to 20 entries
        ops.py trim-playbook-archive --dry-run    — report size without writing
    """
    keep = 50
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == '--keep' and i + 1 < len(args):
            keep = int(args[i + 1]); i += 2
        elif args[i] == '--dry-run':
            dry_run = True; i += 1
        else:
            i += 1

    pid = get_project_id()
    data = api("GET", f"/api/admin/projects/{pid}/directions")
    doc = data.get('design_doc') or {}
    archive = doc.get('playbook_archive') or []

    print(f"Current playbook_archive: {len(archive)} entries")
    before_bytes = len(json.dumps(archive).encode())
    print(f"Archive size: {before_bytes:,} bytes ({before_bytes // 1024} KB)")

    if len(archive) <= keep:
        print(f"Already within cap of {keep} — nothing to trim.")
        return

    trimmed = archive[-keep:]
    after_bytes = len(json.dumps(trimmed).encode())
    dropped = len(archive) - len(trimmed)
    print(f"Would drop {dropped} oldest entries -> {len(trimmed)} remain ({after_bytes:,} bytes)")

    if dry_run:
        print("(dry-run -- no changes written)")
        return

    new_doc = dict(doc)
    new_doc['playbook_archive'] = trimmed
    api("PATCH", f"/api/admin/projects/{pid}", {"design_doc": new_doc})
    print(f"Trimmed archive to {len(trimmed)} entries ({before_bytes - after_bytes:,} bytes freed)")


def _resolve_fingerprint(query: str, project_id: str | None = None) -> dict | None:
    """Find a residual case by fingerprint substring (case-insensitive).

    Fingerprints are long opaque strings; the CLI accepts a unique prefix or
    substring and matches against open cases. Returns None on no/ambiguous
    match (with a printed hint).
    """
    path = "/api/admin/residual-cases?limit=200"
    if project_id:
        path += f"&project_id={project_id}"
    cases = api("GET", path).get("cases", [])
    needle = query.lower()
    matches = [c for c in cases if needle in (c.get("fingerprint") or "").lower()]
    if not matches:
        print(f"No open residual case matches: {query!r}")
        return None
    if len(matches) > 1:
        print(f"Ambiguous — {len(matches)} cases match {query!r}:")
        for c in matches[:5]:
            print(f"  - {c['fingerprint'][:120]}")
        print("Use a longer substring to disambiguate.")
        return None
    return matches[0]


def cmd_fingerprints(args):
    """List or manage residual remediation cases.

    Usage:
        python scripts/ops.py fingerprints
        python scripts/ops.py fingerprints --status IGNORED [--status RESOLVED]
        python scripts/ops.py fingerprints --all                    — every status
        python scripts/ops.py fingerprints --ignore <fp-substring>
        python scripts/ops.py fingerprints --unignore <fp-substring>
        python scripts/ops.py fingerprints --clear                  — wipe in-memory store
    """
    # Mutations first.
    if args and args[0] == '--ignore' and len(args) > 1:
        from blueprint_api import get_project_id
        pid = get_project_id()
        case = _resolve_fingerprint(args[1], pid)
        if not case:
            return
        from urllib.parse import quote
        result = api("POST", f"/api/admin/residual-cases/{quote(case['fingerprint'], safe='')}/ignore?project_id={pid}")
        print(f"Ignored: {result.get('fingerprint', '')[:100]} → status={result.get('status')}")
        return
    if args and args[0] == '--unignore' and len(args) > 1:
        from blueprint_api import get_project_id
        pid = get_project_id()
        # IGNORED cases aren't in the default open-status list, look across all
        ignored_path = f"/api/admin/residual-cases?status=IGNORED&limit=200&project_id={pid}"
        cases = api("GET", ignored_path).get("cases", [])
        needle = args[1].lower()
        matches = [c for c in cases if needle in (c.get("fingerprint") or "").lower()]
        if not matches:
            print(f"No IGNORED case matches: {args[1]!r}")
            return
        if len(matches) > 1:
            print(f"Ambiguous — {len(matches)} IGNORED cases match. Use a longer substring.")
            return
        from urllib.parse import quote
        result = api("POST", f"/api/admin/residual-cases/{quote(matches[0]['fingerprint'], safe='')}/unignore?project_id={pid}")
        print(f"Unignored: {result.get('fingerprint', '')[:100]} → status={result.get('status')}")
        return
    if args and args[0] == '--clear':
        result = api("POST", "/api/admin/residual-fingerprints/clear")
        print(f"Cleared in-memory store: {result}")
        return

    # List mode.
    statuses: list[str] = []
    show_all = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--status' and i + 1 < len(args):
            statuses.append(args[i + 1])
            i += 2
            continue
        if a == '--all':
            show_all = True
            i += 1
            continue
        i += 1
    path = "/api/admin/residual-cases?limit=100"
    if show_all:
        for s in ('ACTIVE', 'REMEDIATING', 'SUSPENDED', 'IGNORED', 'RESOLVED', 'CLOSED_ORGANICALLY', 'MERGED', 'FAILED'):
            path += f"&status={s}"
    else:
        for s in statuses:
            path += f"&status={s}"
    result = api("GET", path)
    cases = result.get("cases", [])
    filt = result.get("status_filter", [])
    print(f"=== RESIDUAL CASES ({len(cases)}) status={','.join(filt)} ===")
    if not cases:
        print("(none)")
        return
    for c in cases:
        fp = (c.get("fingerprint") or "").replace("\n", " ")
        print(f"  [{c['status']:18}] hits={c['hit_count']:<3} consec={c.get('consecutive_failure_count', 0)}  last_seen={(c.get('last_seen_at') or '')[:19]}  pr={c.get('last_spawned_pr_number') or '-'}")
        print(f"    fp: {fp[:200]}")


def cmd_regression_cases(args):
    """List or close regression remediation cases.

    Usage:
        python scripts/ops.py regression-cases
        python scripts/ops.py regression-cases --status ACTIVE
        python scripts/ops.py regression-cases --all
        python scripts/ops.py regression-cases --close <regression_class> [--reason "why"]
        python scripts/ops.py regression-cases --close-prefix <prefix> [--reason "why"]
    """
    from urllib.parse import quote
    from blueprint_api import get_project_id
    pid = get_project_id()

    # Mutations.
    def _build_close_path(cls: str, reason: str | None) -> str:
        # regression_class is a query param so slashes / quotes don't
        # fragment FastAPI's path routing.
        p = f"/api/admin/regression-cases/close?project_id={pid}&regression_class={quote(cls, safe='')}"
        if reason:
            p += f"&reason={quote(reason, safe='')}"
        return p

    if args and args[0] == '--close' and len(args) > 1:
        cls = args[1]
        reason = None
        for i, a in enumerate(args[2:]):
            if a == '--reason' and i + 3 < len(args):
                reason = args[i + 3]
                break
        try:
            result = api("POST", _build_close_path(cls, reason))
            print(f"Closed: {result.get('regression_class', '')[:120]} → {result.get('prior_status')} → {result.get('status')}")
        except Exception as e:
            print(f"Error closing {cls!r}: {e}")
        return
    if args and args[0] == '--close-prefix' and len(args) > 1:
        prefix = args[1]
        reason = None
        for i, a in enumerate(args[2:]):
            if a == '--reason' and i + 3 < len(args):
                reason = args[i + 3]
                break
        # Fetch open cases and filter by prefix
        path = f"/api/admin/regression-cases?project_id={pid}&limit=200"
        for s in ('ACTIVE', 'REMEDIATING', 'NEEDS_MANUAL_INTERVENTION'):
            path += f"&status={s}"
        result = api("GET", path)
        cases = result.get("cases", [])
        matches = [c for c in cases if c.get("regression_class", "").startswith(prefix)]
        if not matches:
            print(f"No open cases match prefix {prefix!r}")
            return
        print(f"Closing {len(matches)} case(s) matching prefix {prefix!r}:")
        for c in matches:
            cls = c["regression_class"]
            try:
                r = api("POST", _build_close_path(cls, reason))
                print(f"  {r.get('prior_status'):28} → {r.get('status'):8}  {cls[:80]}")
            except Exception as e:
                print(f"  ERROR  {cls[:80]}: {e}")
        return

    # List mode.
    statuses = []
    show_all = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--status' and i + 1 < len(args):
            statuses.append(args[i + 1])
            i += 2
            continue
        if a == '--all':
            show_all = True
            i += 1
            continue
        i += 1
    path = f"/api/admin/regression-cases?project_id={pid}&limit=200"
    if show_all:
        for s in ('ACTIVE', 'REMEDIATING', 'NEEDS_MANUAL_INTERVENTION', 'CLOSED', 'MERGED'):
            path += f"&status={s}"
    else:
        for s in statuses:
            path += f"&status={s}"
    result = api("GET", path)
    cases = result.get("cases", [])
    filt = result.get("status_filter", [])
    print(f"=== REGRESSION CASES ({len(cases)}) status={','.join(filt)} ===")
    if not cases:
        print("(none)")
        return
    for c in cases:
        cls = (c.get("regression_class") or "").replace("\n", " ")
        print(f"  [{c['status']:28}] attempts={c.get('attempts',0)}  hits={c.get('hit_count',0)}  last_seen={(c.get('last_seen_at') or '')[:19]}  pr={c.get('last_remediation_pr_number') or '-'}")
        print(f"    class: {cls[:200]}")


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
