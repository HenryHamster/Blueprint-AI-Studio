"""Blueprint pipeline health check.

Surveys current task state across all buckets and surfaces:
  - Stuck tasks (PROVISIONING > 15m, IN_PROGRESS > 90m, EVALUATING > 6h, QUEUED > 2h)
  - Dep-chain breaks (QUEUED tasks waiting on a CANCELLED/FAILED predecessor)
  - Orphaned EVALUATING parents (children all CANCELLED)
  - Orphan PRs (open PRs on github with no live tracking task)
  - Recent FAILED tasks (last N hours)
  - Active task summary by type

Usage:
  python scripts/pipeline_health.py
  python scripts/pipeline_health.py --hours 24             # FAILED window
  python scripts/pipeline_health.py --include-completed    # also enumerate completed tasks
  python scripts/pipeline_health.py --quiet                # summary only
  python scripts/pipeline_health.py --check-prs            # gh pr list cross-check (slower)
  python scripts/pipeline_health.py --completed-pages 5    # widen COMPLETED window for dep lookup
  python scripts/pipeline_health.py --config <path>        # alternate .blueprint-config.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / '.blueprint-config.json'

ALL_STATUSES = ('IN_PROGRESS', 'PROVISIONING', 'QUEUED', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED')
ACTIVE_STATUSES = ('IN_PROGRESS', 'PROVISIONING', 'QUEUED', 'EVALUATING')

# Stuck thresholds in seconds (last-update age).
STUCK_THRESHOLDS_SEC = {
    'PROVISIONING': 15 * 60,        # VM should claim within 15m
    'IN_PROGRESS': 90 * 60,         # task should heartbeat / progress within 90m
    'EVALUATING': 6 * 3600,         # parent re-eval should not stall > 6h
    'QUEUED': 2 * 3600,             # queued > 2h means dispatcher backlog
}


def _load_config(path: Path) -> dict:
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def _api_get(api_url: str, api_key: str, path: str, params: dict | None = None) -> dict:
    url = api_url.rstrip('/') + path
    if params:
        from urllib.parse import urlencode
        url += '?' + urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {api_key}'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f'API error {exc.code} for {path}: {exc.read().decode("utf-8", "replace")[:200]}\n')
        return {}
    except urllib.error.URLError as exc:
        sys.stderr.write(f'API URL error for {path}: {exc.reason}\n')
        return {}


def _parse_ts(s: str | None) -> datetime | None:
    """Parse an ISO timestamp from the backend.

    Backend timestamps are *naive UTC* (per the TIMESTAMPTZ migration backlog),
    so attach UTC tzinfo if missing.
    """
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _age_seconds(ts_str: str | None, now: datetime) -> float | None:
    ts = _parse_ts(ts_str)
    if not ts:
        return None
    return (now - ts).total_seconds()


def _last_status_age(t: dict, now: datetime) -> float | None:
    """The listing endpoint omits updated_at — derive from the last
    status_log entry's `t` timestamp instead. Fall back to top-level
    updated_at/created_at if status_log is empty."""
    log = t.get('status_log') or []
    if isinstance(log, list) and log:
        last = log[-1]
        if isinstance(last, dict):
            ts = last.get('t') or last.get('timestamp')
            if ts:
                return _age_seconds(ts, now)
    return _age_seconds(t.get('updated_at') or t.get('created_at'), now)


def _fmt_age(sec: float | None) -> str:
    if sec is None:
        return '?'
    if sec < 90:
        return f'{int(sec)}s'
    if sec < 90 * 60:
        return f'{sec / 60:.1f}m'
    if sec < 36 * 3600:
        return f'{sec / 3600:.1f}h'
    return f'{sec / 86400:.1f}d'


def _fetch_status(api_url: str, api_key: str, status: str, limit: int = 200) -> list[dict]:
    data = _api_get(api_url, api_key, '/api/admin/tasks', {'status': status, 'limit': limit})
    return data.get('tasks', [])


def _format_task_line(t: dict, now: datetime) -> str:
    tid = (t.get('id') or '')[:8]
    ttype = (t.get('task_type') or '')[:12]
    title = (t.get('title') or '')[:60]
    upd = _last_status_age(t, now)
    pr = t.get('pr_url') or ''
    pr_short = pr.rsplit('/', 1)[-1] if pr else ''
    parent = (t.get('parent_id') or '')[:8]
    return f'  {tid} {ttype:12s} upd={_fmt_age(upd):>6s} parent={parent:8s} pr={pr_short:>5s}  {title}'


def stuck_check(active_by_status: dict[str, list[dict]], now: datetime) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for status, threshold_sec in STUCK_THRESHOLDS_SEC.items():
        for t in active_by_status.get(status, []):
            age = _last_status_age(t, now)
            if age is not None and age > threshold_sec:
                out.append((status, t))
    return out


def orphan_evaluating_parents(evaluating: list[dict], all_tasks: dict[str, list[dict]]) -> list[dict]:
    """Identify EVALUATING parents whose children are ALL non-active and ALL CANCELLED.

    These are research/plan parents waiting on children that will never arrive.
    """
    children_by_parent: dict[str, list[dict]] = defaultdict(list)
    for status_tasks in all_tasks.values():
        for t in status_tasks:
            pid = t.get('parent_id')
            if pid:
                children_by_parent[pid].append(t)

    orphans: list[dict] = []
    for parent in evaluating:
        kids = children_by_parent.get(parent.get('id') or '', [])
        if not kids:
            # No children at all — could be normal (parent being re-evaluated mid-spawn)
            # or genuinely orphaned. Flag if EVALUATING > 1h with zero kids.
            continue
        statuses = {k.get('status') for k in kids}
        active_kids = statuses & set(ACTIVE_STATUSES)
        if not active_kids and statuses == {'CANCELLED'}:
            orphans.append(parent)
    return orphans


def recent_failures(api_url: str, api_key: str, hours: int) -> list[dict]:
    failed = _fetch_status(api_url, api_key, 'FAILED', limit=200)
    now = datetime.now(timezone.utc)
    cutoff_sec = hours * 3600
    return [t for t in failed if (_last_status_age(t, now) or 0) <= cutoff_sec]


_DEP_RE = re.compile(r'Waiting on \d+ deps?:\s*([0-9a-f]{8}(?:[0-9a-f-]{28})?)')


def parse_dep_id(t: dict) -> str | None:
    """Extract the dep task-id from a 'Waiting on N dep: <id>' status_message."""
    msg = t.get('latest_status_message') or ''
    m = _DEP_RE.search(msg)
    return m.group(1) if m else None


def dep_chain_breaks(api_url: str, api_key: str, queued: list[dict],
                      task_index: dict[str, dict],
                      completed_pages: int) -> list[tuple[dict, str, str]]:
    """For each QUEUED task whose status_message names a dep, look up the dep
    and report tasks blocked on a CANCELLED/FAILED predecessor (real break)
    or on a COMPLETED predecessor (transient — dep checker hasn't run yet).

    Returns list of (queued_task, dep_id, dep_status_or_'NOT_FOUND').
    Adds to task_index in-place when COMPLETED tasks are paged in for lookups.
    """
    out: list[tuple[dict, str, str]] = []
    # Lazy-paginate COMPLETED if we miss a dep prefix on first try.
    fetched_pages = 0
    for q in queued:
        dep_id = parse_dep_id(q)
        if not dep_id:
            continue
        # Resolve dep — try direct GET first (handles full UUID).
        if len(dep_id) == 36 and dep_id in task_index:
            dep = task_index[dep_id]
        else:
            dep = _resolve_task(api_url, api_key, dep_id, task_index)
            while dep is None and fetched_pages < completed_pages:
                # Widen COMPLETED window in chunks of 200.
                limit = 200 * (fetched_pages + 2)
                more = _fetch_status(api_url, api_key, 'COMPLETED', limit=limit)
                for t in more:
                    task_index.setdefault(t['id'], t)
                fetched_pages += 1
                dep = _resolve_task(api_url, api_key, dep_id, task_index)
        if dep is None:
            out.append((q, dep_id, 'NOT_FOUND'))
        else:
            dep_status = dep.get('status') or '?'
            if dep_status in {'CANCELLED', 'FAILED'}:
                out.append((q, dep.get('id') or dep_id, dep_status))
            elif dep_status == 'COMPLETED':
                # Transient — dep checker hasn't promoted q to runnable yet.
                # Still surface it so operators know to nudge the dep checker
                # if it lingers (>30m past dep COMPLETED is a real bug).
                out.append((q, dep.get('id') or dep_id, dep_status))
    return out


def _resolve_task(api_url: str, api_key: str, prefix_or_id: str,
                   task_index: dict[str, dict]) -> dict | None:
    if len(prefix_or_id) == 36:
        # full UUID — try GET
        if prefix_or_id in task_index:
            return task_index[prefix_or_id]
        try:
            data = _api_get(api_url, api_key, f'/api/admin/tasks/{prefix_or_id}')
            if data and 'id' in data:
                task_index[prefix_or_id] = data
                return data
        except Exception:
            pass
        return None
    # 8-char prefix — search the index
    matches = [t for tid, t in task_index.items() if tid.startswith(prefix_or_id)]
    return matches[0] if matches else None


def fetch_open_prs(repo: str) -> list[dict]:
    """gh pr list --state open --limit 200 — returns [{number, url, title, state}]."""
    try:
        out = subprocess.run(
            ['gh', 'pr', 'list', '--repo', repo, '--state', 'open', '--limit', '200',
             '--json', 'number,url,title,state,headRefName,createdAt'],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return json.loads(out.stdout or '[]')
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f'gh pr list failed: {exc}\n')
        return []


_PR_NUM_RE = re.compile(r'PR\s*#(\d+)', re.IGNORECASE)


def orphan_prs(open_prs: list[dict], by_status: dict[str, list[dict]]) -> list[dict]:
    """Open PRs with no IN_PROGRESS/PROVISIONING/QUEUED/EVALUATING task tracking them.

    Cross-reference by:
      1. task.pr_url match (definitive when set)
      2. PR# parsed from task.title ("Review PR #4084", "Fix PR #4085")
      3. branch match `blueprint/task-<task-id-prefix>` ↔ active task id

    A PR is orphaned only if NONE of those resolve.
    """
    active_pr_urls: set[str] = set()
    active_pr_numbers: set[int] = set()
    active_task_id_prefixes: set[str] = set()
    for s in ('IN_PROGRESS', 'PROVISIONING', 'QUEUED', 'EVALUATING'):
        for t in by_status.get(s, []):
            url = t.get('pr_url')
            if url:
                active_pr_urls.add(url)
            title = t.get('title') or ''
            m = _PR_NUM_RE.search(title)
            if m:
                active_pr_numbers.add(int(m.group(1)))
            tid = t.get('id') or ''
            if tid:
                active_task_id_prefixes.add(tid[:8])
    out: list[dict] = []
    for pr in open_prs:
        if pr.get('url') in active_pr_urls:
            continue
        if pr.get('number') in active_pr_numbers:
            continue
        branch = pr.get('headRefName') or ''
        # blueprint/task-<8char-prefix>...   or  research/<8char-prefix>
        m = re.match(r'(?:blueprint/task-|research/)([0-9a-f]{8})', branch)
        if m and m.group(1) in active_task_id_prefixes:
            continue
        out.append(pr)
    return out


def task_type_summary(active_by_status: dict[str, list[dict]]) -> Counter:
    c: Counter = Counter()
    for status in ACTIVE_STATUSES:
        for t in active_by_status.get(status, []):
            c[(status, t.get('task_type') or '?')] += 1
    return c


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Blueprint pipeline health check')
    p.add_argument('--config', default=str(DEFAULT_CONFIG), help='Path to .blueprint-config.json')
    p.add_argument('--hours', type=int, default=12, help='FAILED window in hours (default 12)')
    p.add_argument('--include-completed', action='store_true', help='Include COMPLETED snapshot')
    p.add_argument('--quiet', action='store_true', help='Summary only, skip per-task lines')
    p.add_argument('--limit', type=int, default=200, help='Per-status fetch limit')
    p.add_argument('--check-prs', action='store_true', help='Also cross-check open PRs vs tasks (uses gh)')
    p.add_argument('--completed-pages', type=int, default=2, help='How many COMPLETED pages of 200 to scan for dep-chain lookups')
    p.add_argument('--repo', default=None, help='owner/repo for --check-prs (default: from config.default_repo)')
    args = p.parse_args(argv)

    cfg = _load_config(Path(args.config))
    api_url = cfg['api_url']
    api_key = cfg['api_key']
    repo = args.repo or cfg.get('default_repo')

    statuses = list(ALL_STATUSES) if args.include_completed else [s for s in ALL_STATUSES if s != 'COMPLETED']
    by_status: dict[str, list[dict]] = {}
    for s in statuses:
        by_status[s] = _fetch_status(api_url, api_key, s, limit=args.limit)

    now = datetime.now(timezone.utc)

    print('# Pipeline health snapshot')
    print(f'# {now.isoformat()}')
    print()
    print('## Status counts')
    for s in statuses:
        print(f'  {s:14s} {len(by_status[s]):>4}')
    print()

    # Active task type breakdown
    type_summary = task_type_summary(by_status)
    if type_summary:
        print('## Active task types')
        for (status, ttype), n in sorted(type_summary.items()):
            print(f'  {status:14s} {ttype:14s} {n:>3}')
        print()

    # Stuck check
    stuck = stuck_check(by_status, now)
    print(f'## Stuck tasks ({len(stuck)})')
    if not stuck:
        print('  (none — thresholds: PROV>15m, IN_PROGRESS>90m, EVAL>6h, QUEUED>2h)')
    else:
        for status, t in stuck:
            print(f'  [{status}]')
            print(_format_task_line(t, now))
            sm = (t.get('latest_status_message') or '').replace('\n', ' | ')[:160]
            if sm:
                print(f'    sm: {sm}')
    print()

    # Orphan EVALUATING parents
    if 'EVALUATING' in by_status:
        orphans = orphan_evaluating_parents(by_status['EVALUATING'], by_status)
        print(f'## EVALUATING parents with all-CANCELLED children ({len(orphans)})')
        if not orphans:
            print('  (none)')
        else:
            for parent in orphans:
                print(_format_task_line(parent, now))
        print()

    # Dep-chain breaks (QUEUED waiting on dead/completed predecessor)
    task_index: dict[str, dict] = {}
    for tasks in by_status.values():
        for t in tasks:
            tid = t.get('id')
            if tid:
                task_index[tid] = t
    breaks = dep_chain_breaks(api_url, api_key, by_status.get('QUEUED', []),
                                task_index, args.completed_pages)
    print(f'## Dep-chain checks on QUEUED ({len(breaks)})')
    if not breaks:
        print('  (no QUEUED tasks reference a dep, or all deps are still progressing)')
    else:
        for q, dep_id, dep_status in breaks:
            tag = ''
            if dep_status in {'CANCELLED', 'FAILED'}:
                tag = '!!BROKEN_CHAIN'
            elif dep_status == 'COMPLETED':
                age = _last_status_age(q, now)
                if age and age > 30 * 60:
                    tag = '!!STALE_DEP_CHECKER (dep COMPLETED but task still QUEUED — dep-checker drift)'
                else:
                    tag = 'transient (dep just completed; should promote shortly)'
            elif dep_status == 'NOT_FOUND':
                tag = '!!DEP_NOT_FOUND (try --completed-pages 5+)'
            print(f'  {(q.get("id") or "")[:8]} -> dep {dep_id[:8]} ({dep_status}) {tag}')
            sm = (q.get('latest_status_message') or '').replace('\n', ' | ')[:120]
            if sm:
                print(f'    sm: {sm}')
    print()

    # Orphan PRs (gh cross-check)
    if args.check_prs:
        if not repo:
            print('## Orphan PRs')
            print('  (skipped — no --repo given and config.default_repo unset)')
            print()
        else:
            open_prs = fetch_open_prs(repo)
            orphans_pr = orphan_prs(open_prs, by_status)
            print(f'## Open PRs without an active tracking task ({len(orphans_pr)} of {len(open_prs)} open)')
            if not orphans_pr:
                print('  (every open PR has a live tracking task)')
            else:
                for pr in orphans_pr:
                    title = (pr.get('title') or '')[:70]
                    branch = pr.get('headRefName', '')
                    cre = _age_seconds(pr.get('createdAt'), now)
                    print(f'  PR#{pr.get("number"):>4}  age={_fmt_age(cre):>6}  br={branch:<35} {title}')
            print()

    # Active per-status detail (unless --quiet)
    if not args.quiet:
        for s in ACTIVE_STATUSES:
            tasks = by_status.get(s, [])
            if not tasks:
                continue
            print(f'## {s} detail')
            for t in tasks:
                print(_format_task_line(t, now))
            print()

    # Recent FAILED
    print(f'## FAILED in last {args.hours}h')
    cutoff_sec = args.hours * 3600
    recent = [t for t in by_status.get('FAILED', []) if (_last_status_age(t, now) or 0) <= cutoff_sec]
    if not recent:
        print('  (none)')
    else:
        for t in recent:
            print(_format_task_line(t, now))
            sm = (t.get('latest_status_message') or '').replace('\n', ' | ')[:200]
            if sm:
                print(f'    sm: {sm}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
