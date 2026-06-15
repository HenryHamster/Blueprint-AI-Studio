#!/usr/bin/env python3
"""blueprint — unified Blueprint operator CLI.

Single entrypoint over the Blueprint admin API for day-to-day operation.
Reuses :mod:`blueprint_api` as the only HTTP layer (and therefore the only
auth path): ``blueprint_api.load_config()`` resolves the admin key (via
BLUEPRINT_API_KEY / .blueprint-config.json / gcloud bootstrap) or an
agent-JWT read-only fallback (ORCHESTRATOR_URL + AGENT_SECRET_TOKEN). No new
credential path is introduced here.

Every read command is human-readable by default and emits machine JSON when
the global ``--json`` flag is passed (before or after the subcommand).

Subcommand groups:
    status            current version phase, deploy state, promotion eligibility
    tasks list|show   list/filter tasks, or show one by id
    versions list|show  release versions with phase/status/sha and a summary
    promote-recover   promotion + recovery operator levers (confirm before mutate)
    logs tail|errors|search|task   Cloud Logging reads (via gcloud)

Examples:
    python scripts/blueprint.py status
    python scripts/blueprint.py --json status
    python scripts/blueprint.py tasks list --status IN_PROGRESS --type create
    python scripts/blueprint.py tasks show 7ab3f93a
    python scripts/blueprint.py versions show
    python scripts/blueprint.py promote-recover qualify --yes
    python scripts/blueprint.py logs errors --minutes 30
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from blueprint_api import api, get_project_id, load_config

SERVICE_NAME = os.environ.get("BLUEPRINT_GCP_SERVICE", "blueprint-orchestrator")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "blueprint-orchestrator")


# ── output helpers ───────────────────────────────────────────────────────────

def _json_mode(args: argparse.Namespace) -> bool:
    """True when the global --json flag was passed (before or after subcommand)."""
    return bool(getattr(args, "json_out", False))


def _emit(args: argparse.Namespace, data, render) -> None:
    """Emit ``data`` as JSON (``--json``) or via the ``render`` human formatter."""
    if _json_mode(args):
        print(json.dumps(data, indent=2, default=str))
    else:
        render(data)


def _safe_read(path: str) -> tuple:
    """GET ``path`` returning ``(data, error)``; never raises.

    Lets a composed read (e.g. ``status``) degrade gracefully when a single
    underlying endpoint is unavailable instead of aborting the whole command.
    """
    try:
        return api("GET", path), None
    except Exception as exc:  # noqa: BLE001 — surface any transport/HTTP error as text
        return None, str(exc)


def first_payload(task: dict) -> dict:
    """First payload element of an API task dict.

    Mirrors the backend ``Task.first_payload`` accessor for the JSON the API
    returns (the ORM property is not available client-side). Used for fields
    that live on the payload (e.g. ``pr_number``); ``task_type``/``status`` are
    already promoted to the top level by the admin serializer.
    """
    payloads = task.get("payloads") or [{}]
    return payloads[0] if payloads else {}


# ── status ───────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    """Fetch canonical system state from GET /api/admin/state."""
    pid = get_project_id(getattr(args, "project_id", None))
    state, state_err = _safe_read(f"/api/admin/state?project_id={pid}")
    state = state or {}
    deploy = state.get("deploy") or {}
    version = state.get("version")

    data = {
        "project_id": pid,
        "deploy_state": {
            "status": deploy.get("status"),
            "current_revision": deploy.get("current_revision"),
            "pause_reason": deploy.get("pause_reason"),
            "rollback_reason": deploy.get("rollback_reason"),
        },
        "current_version": (
            {
                "version_number": version.get("version_number"),
                "phase": version.get("phase"),
                "qualification_state": version.get("qualification_state"),
                "merged_task_count": version.get("merged_task_count"),
                "active_candidate_sha": (version.get("serving") or {}).get("sha"),
            }
            if version
            else None
        ),
        "promotion": {
            "enabled_effective": None,
            "enabled_source": None,
            "ready": None,
            "reason": None,
        },
        "errors": {
            k: v
            for k, v in (("state", state_err),)
            if v
        },
    }
    _emit(args, data, _render_status)


def _render_status(data: dict) -> None:
    ds = data["deploy_state"]
    print("=== DEPLOY STATE ===")
    print(f"  Status:    {ds.get('status', '?')}")
    print(f"  Revision:  {ds.get('current_revision', '?')}")
    if ds.get("pause_reason"):
        print(f"  Paused:    {ds['pause_reason']}")
    if ds.get("rollback_reason"):
        print(f"  Rollback:  {ds['rollback_reason']}")

    cv = data.get("current_version")
    print("\n=== CURRENT VERSION ===")
    if not cv:
        print("  (no open version)")
    else:
        qs = cv.get("qualification_state")
        print(f"  v{cv.get('version_number')}  phase={cv.get('phase')}  "
              f"state={qs}")
        print(f"  merged_tasks={cv.get('merged_task_count')}  "
              f"candidate={(cv.get('active_candidate_sha') or '-')[:12]}")

    errors = data.get("errors") or {}
    if errors:
        print("\n=== READ ERRORS (partial status) ===")
        for endpoint, err in errors.items():
            print(f"  {endpoint}: {err}")


# ── tasks ──────────────────────────────────────────────────────────────────--

def cmd_tasks(args: argparse.Namespace) -> None:
    """Dispatch ``tasks list`` / ``tasks show`` (defaults to list)."""
    sub = getattr(args, "tasks_cmd", None) or "list"
    if sub == "show":
        _tasks_show(args)
    else:
        _tasks_list(args)


def _tasks_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    path = f"/api/admin/tasks?project_id={pid}&limit={getattr(args, 'limit', 50)}"
    if getattr(args, "status", None):
        path += f"&status={args.status}"
    if getattr(args, "task_type", None):
        path += f"&task_type={args.task_type}"
    if getattr(args, "direction", None):
        path += f"&direction_id={args.direction}"
    if getattr(args, "search", None):
        from urllib.parse import quote

        path += f"&search={quote(args.search)}"

    # IMPORTANT API GAP: the Task model has NO release-version linkage (its
    # `version` column is an optimistic-lock counter, not a release version),
    # so the API has no task-by-version filter. Surface this explicitly rather
    # than silently returning wrong results.
    if getattr(args, "version", None):
        print(
            "NOTICE: version filtering is unsupported by the API — the Task "
            "model has no release-version linkage. Ignoring --version and "
            "returning all matching tasks.",
            file=sys.stderr,
        )

    resp = api("GET", path)
    tasks = resp.get("tasks", resp) if isinstance(resp, dict) else resp
    total = resp.get("total") if isinstance(resp, dict) else None
    _emit(args, {"tasks": tasks, "total": total}, _render_task_list)


def _render_task_list(data: dict) -> None:
    tasks = data.get("tasks") or []
    total = data.get("total")
    header = f"=== TASKS ({len(tasks)} shown" + (f" / {total} total" if total is not None else "") + ") ==="
    print(header)
    if not tasks:
        print("  (none)")
        return
    for t in tasks:
        tid = (t.get("id") or "?")[:8]
        ttype = t.get("task_type", "?")
        status = t.get("status", "?")
        title = (t.get("title") or "")[:50]
        msg = (t.get("latest_status_message") or "")[:70]
        line = f"  {tid}  [{ttype:8}] {status:13} {title:<50}"
        if msg:
            line += f"  | {msg}"
        print(line)


def _tasks_show(args: argparse.Namespace) -> None:
    task = api("GET", f"/api/admin/tasks/{args.task_id}")
    _emit(args, task, _render_task_detail)


def _render_task_detail(t: dict) -> None:
    print(f"=== TASK {t.get('id', '?')} ===")
    print(f"  Title:    {t.get('title', '?')}")
    print(f"  Type:     {t.get('task_type', '?')}")
    print(f"  Status:   {t.get('status', '?')}")
    print(f"  Created:  {(t.get('created_at') or '?')[:19]}")
    parent = t.get("parent_task_id")
    if parent:
        print(f"  Parent:   {parent}")
    pr = t.get("pr_url") or first_payload(t).get("pr_number")
    if pr:
        print(f"  PR:       {pr}")
    msg = t.get("latest_status_message")
    if msg:
        print(f"  Message:  {msg[:200]}")
    desc = t.get("description")
    if desc:
        print(f"\n  Description:\n    {desc[:500]}")


# ── versions ──────────────────────────────────────────────────────────────--

def cmd_versions(args: argparse.Namespace) -> None:
    """Dispatch ``versions list`` / ``versions show`` (defaults to list)."""
    pid = get_project_id(getattr(args, "project_id", None))
    lifecycle = api("GET", f"/api/admin/projects/{pid}/version-lifecycle")
    cv = lifecycle.get("current_version")
    recent = lifecycle.get("recent_versions") or []
    sub = getattr(args, "versions_cmd", None) or "list"

    if sub == "show":
        target = getattr(args, "version_number", None)
        ver = cv
        if target is not None:
            ver = None
            if cv and str(cv.get("version_number")) == str(target):
                ver = cv
            else:
                for v in recent:
                    if str(v.get("version_number")) == str(target):
                        ver = v
                        break
        if not ver:
            msg = (f"version {target} not found"
                   if target is not None else "no open version")
            _emit(args, {"error": msg}, lambda d: print(d["error"]))
            return
        summary = {
            "version_number": ver.get("version_number"),
            "phase": ver.get("phase"),
            "status": ver.get("status", "open" if ver is cv else None),
            "promoted_sha": ver.get("promoted_sha") or ver.get("served_sha"),
            "merged_task_count": ver.get("merged_task_count"),
            "qualification_state": ver.get("qualification_state"),
            "acceptance_aggregate": ver.get("acceptance_aggregate"),
            "goal": ver.get("goal"),
        }
        _emit(args, summary, _render_version_summary)
        return

    data = {
        "current_version": cv,
        "recent_versions": recent,
        "promotion_enabled_effective": lifecycle.get("promotion_enabled_effective"),
    }
    _emit(args, data, _render_version_list)


def _version_line(v: dict, marker: str = " ") -> str:
    num = v.get("version_number", "?")
    phase = v.get("phase", "?")
    status = v.get("status", "open")
    sha = (v.get("promoted_sha") or v.get("served_sha") or "-")[:12]
    merged = v.get("merged_task_count")
    merged_str = f"  merged={merged}" if merged is not None else ""
    return f"  {marker} v{num}  [{status:9}] phase={phase:9} sha={sha}{merged_str}"


def _render_version_list(data: dict) -> None:
    print("=== RELEASE VERSIONS ===")
    cv = data.get("current_version")
    if cv:
        print(_version_line(cv, marker="*"))
    else:
        print("  (no open version)")
    recent = data.get("recent_versions") or []
    if recent:
        print("\n  --- Recent ---")
        for v in recent:
            print(_version_line(v))


def _render_version_summary(s: dict) -> None:
    print(f"=== VERSION v{s.get('version_number')} SUMMARY ===")
    print(f"  phase:        {s.get('phase')}")
    print(f"  status:       {s.get('status')}")
    print(f"  sha:          {(s.get('promoted_sha') or '-')[:12]}")
    print(f"  merged_tasks: {s.get('merged_task_count')}")
    print(f"  qual_state:   {s.get('qualification_state')}")
    agg = s.get("acceptance_aggregate")
    if agg:
        print(f"  acceptance:   {json.dumps(agg)}")
    if s.get("goal"):
        print(f"  goal:         {s['goal'][:200]}")


# ── promote-recover ──────────────────────────────────────────────────────────

def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Interactive y/N confirmation; bypassed by ``--yes``.

    Without ``--yes`` on a non-interactive stdin we refuse (returns False) so a
    mutation never blocks a script waiting on input that will never arrive.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to mutate without --yes (stdin is not interactive).",
              file=sys.stderr)
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


# action -> (HTTP method, path-template, summary, body-builder)
# path-template is formatted with {pid}. body-builder takes args -> dict|None.
_PROMOTE_RECOVER_ACTIONS = {
    # Promotion side
    "arm": ("POST", "/api/admin/projects/{pid}/promotion-enabled",
            "arm live promotion for this project",
            lambda a: {"enabled": True}),
    "hold": ("POST", "/api/admin/projects/{pid}/promotion-enabled",
             "hold (disable) live promotion for this project",
             lambda a: {"enabled": False}),
    "clear-override": ("POST", "/api/admin/projects/{pid}/promotion-enabled",
                       "clear this project's promotion override (fall back to fleet)",
                       lambda a: {"enabled": None}),
    "cut": ("POST", "/api/admin/projects/{pid}/version-cut",
            "abandon the open version and open the next",
            lambda a: {"reason": getattr(a, "reason", None)}),
    "qualify": ("POST", "/api/admin/projects/{pid}/qualify",
                "trigger a qualification run for the open version",
                lambda a: {}),
    # Recovery side
    "clear-deploy-failed": ("POST", "/api/admin/projects/{pid}/clear-deploy-failed",
                            "clear a stale deploy_failed serving state",
                            lambda a: {"force": getattr(a, "force", False),
                                       "reason": getattr(a, "reason", None)}),
    "rollback": ("POST", "/api/admin/deploy-state/rollback",
                 "roll back to the last known-good revision (pauses pipeline)",
                 lambda a: {}),
    "mark-stable": ("POST", "/api/admin/deploy-state/mark-stable",
                    "mark the current deploy as stable",
                    lambda a: {}),
    "resume": ("POST", "/api/admin/deploy-state/resume",
               "resume the pipeline",
               lambda a: {}),
}


def cmd_promote_recover(args: argparse.Namespace) -> None:
    """Run a promotion/recovery lever with a confirmation prompt before mutating."""
    action = args.action
    spec = _PROMOTE_RECOVER_ACTIONS[action]
    method, path_tmpl, summary, build_body = spec

    cfg = load_config()
    if cfg.get("read_only"):
        print(
            f"ERROR: '{action}' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)

    pid = get_project_id(getattr(args, "project_id", None))
    path = path_tmpl.format(pid=pid)
    body = build_body(args)

    if not _confirm(f"About to {summary}. Proceed?", getattr(args, "yes", False)):
        print("Aborted.")
        return

    result = api(method, path, body if body else {})
    _emit(args, {"action": action, "result": result}, _render_promote_recover)


def _render_promote_recover(data: dict) -> None:
    print(f"=== {data['action']} ===")
    print(json.dumps(data.get("result"), indent=2, default=str))


# ── logs (ported from scripts/logs.py) ───────────────────────────────────────

def _resolve_gcloud() -> str | None:
    """Locate a working gcloud binary, or None if unavailable.

    Prefers PATH (shutil.which) then a few common SDK install locations, the
    same graceful-degradation contract scripts/logs.py provides.
    """
    candidates = [
        shutil.which("gcloud"),
        os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
        "gcloud",
    ]
    for path in candidates:
        if not path:
            continue
        try:
            probe = subprocess.run(
                [path, "--version"], capture_output=True, timeout=15
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if probe.returncode == 0:
            return path
    return None


def _fetch_logs(filter_str: str, limit: int) -> list:
    """Fetch Cloud Logging entries via ``gcloud logging read``.

    Returns a list of LogEntry dicts (newest-first), or an empty list on any
    failure (gcloud missing, unauthenticated, timeout, parse error) — printing
    actionable guidance to stderr so the caller degrades gracefully.
    """
    gcloud = _resolve_gcloud()
    if not gcloud:
        print("ERROR: gcloud not found. Install the Google Cloud SDK and run "
              "`gcloud auth login`.", file=sys.stderr)
        return []
    cmd = [
        gcloud, "logging", "read", filter_str,
        f"--project={GCP_PROJECT}",
        f"--limit={limit}",
        "--format=json",
        "--order=desc",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        print("ERROR: `gcloud logging read` timed out after 90s.", file=sys.stderr)
        return []
    except (FileNotFoundError, OSError) as exc:
        print(f"ERROR: could not run gcloud: {exc}", file=sys.stderr)
        return []
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        print(f"ERROR: `gcloud logging read` failed (exit {result.returncode}):",
              file=sys.stderr)
        print(f"  {stderr[:500]}", file=sys.stderr)
        if "auth" in stderr.lower() or "credential" in stderr.lower():
            print("  Run: gcloud auth login", file=sys.stderr)
        return []
    out = (result.stdout or "").strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        start = out.find("[")
        if start != -1:
            try:
                return json.loads(out[start:])
            except json.JSONDecodeError:
                pass
        print(f"ERROR: could not parse gcloud output as JSON:\n  {out[:300]}",
              file=sys.stderr)
        return []


def _format_entry(entry: dict) -> tuple:
    """Normalize a LogEntry to ``(timestamp, severity, text)``."""
    ts = (entry.get("timestamp") or "")[:19]
    severity = (entry.get("severity") or "?")[:5]
    text = entry.get("textPayload", "")
    if not text:
        json_payload = entry.get("jsonPayload", {})
        text = json_payload.get("message", json.dumps(json_payload) if json_payload else "")
    if not text:
        http = entry.get("httpRequest", {})
        if http:
            text = (f"{http.get('requestMethod', '?')} "
                    f"{http.get('requestUrl', '?')} -> {http.get('status', '?')}")
    return ts, severity, text


def _time_filter(minutes: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return f'timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"'


def _base_filter() -> str:
    return (f'resource.type="cloud_run_revision" '
            f'resource.labels.service_name="{SERVICE_NAME}" ')


def cmd_logs(args: argparse.Namespace) -> None:
    """Tail / search / fetch Cloud Run logs (errors, tail, search, task)."""
    sub = getattr(args, "logs_cmd", None)
    if not sub:
        print("Usage: blueprint logs {tail|errors|search|task} ...", file=sys.stderr)
        sys.exit(1)

    minutes = getattr(args, "minutes", None)
    limit = getattr(args, "limit", None)

    if sub == "errors":
        minutes = minutes or 60
        limit = limit or 15
        filter_str = _base_filter() + f"severity>=ERROR {_time_filter(minutes)}"
    elif sub == "tail":
        minutes = minutes or 10
        limit = limit or 30
        severity = (getattr(args, "severity", None) or "INFO").upper()
        filter_str = _base_filter() + f"severity>={severity} {_time_filter(minutes)}"
    elif sub == "search":
        minutes = minutes or 60
        limit = limit or 20
        filter_str = _base_filter() + f'textPayload:"{args.pattern}" {_time_filter(minutes)}'
    elif sub == "task":
        minutes = minutes or 120
        limit = limit or 50
        filter_str = _base_filter() + f'textPayload:"{args.task_id}" {_time_filter(minutes)}'
    else:  # pragma: no cover — argparse restricts choices
        print(f"Unknown logs subcommand: {sub}", file=sys.stderr)
        sys.exit(1)

    entries = _fetch_logs(filter_str, limit)
    full = getattr(args, "full", False)
    formatted = [
        {"timestamp": ts, "severity": sev, "text": text}
        for ts, sev, text in (_format_entry(e) for e in entries)
    ]
    _emit(args, {"subcommand": sub, "minutes": minutes, "entries": formatted},
          lambda d: _render_logs(d, full))


def _render_logs(data: dict, full: bool) -> None:
    entries = data.get("entries") or []
    if not entries:
        print(f"No matching logs in the last {data.get('minutes')} minutes.")
        return
    print(f"=== LOGS ({data.get('subcommand')}, last {data.get('minutes')}min, "
          f"{len(entries)} entries) ===")
    # gcloud returns newest-first; print oldest-first for natural reading order.
    for e in reversed(entries):
        ts, sev, text = e["timestamp"], e["severity"], e["text"]
        if full:
            print(f"  {ts}  [{sev:5}] {text}")
        else:
            print(f"  {ts}  [{sev:5}] {text.splitlines()[0] if text else ''}")


# ── argument parser ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Construct the full argparse tree. Every subcommand exposes --help."""
    # Shared parent so --json works before OR after the subcommand. SUPPRESS
    # default means an absent flag on one parser never clobbers a True set on
    # another (the classic argparse parent-subparser override gotcha).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", dest="json_out", action="store_true",
                        default=argparse.SUPPRESS,
                        help="Emit machine-readable JSON instead of human text.")
    common.add_argument("--project-id", dest="project_id", default=None,
                        help="Project id (defaults to the first/configured project).")

    parser = argparse.ArgumentParser(
        prog="blueprint",
        description="Unified Blueprint operator CLI (status/tasks/versions/"
                    "promote-recover/logs).",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", parents=[common],
                   help="Version phase, deploy state, and promotion eligibility.")

    # tasks
    p_tasks = sub.add_parser("tasks", parents=[common], help="List or show tasks.")
    tasks_sub = p_tasks.add_subparsers(dest="tasks_cmd")
    p_tasks_list = tasks_sub.add_parser("list", parents=[common], help="List/filter tasks.")
    p_tasks_list.add_argument("--status", help="Filter by task status (e.g. IN_PROGRESS).")
    p_tasks_list.add_argument("--type", dest="task_type", help="Filter by task type.")
    p_tasks_list.add_argument("--direction", help="Filter by direction id.")
    p_tasks_list.add_argument("--search", help="Substring search over title/desc/message.")
    p_tasks_list.add_argument("--limit", type=int, default=50, help="Max tasks (default 50).")
    p_tasks_list.add_argument("--version", help="UNSUPPORTED by the API — prints a notice "
                                                "(Task has no release-version linkage).")
    p_tasks_show = tasks_sub.add_parser("show", parents=[common], help="Show one task by id.")
    p_tasks_show.add_argument("task_id", help="Task id (full uuid).")

    # versions
    p_versions = sub.add_parser("versions", parents=[common],
                                help="List release versions or show a summary.")
    versions_sub = p_versions.add_subparsers(dest="versions_cmd")
    versions_sub.add_parser("list", parents=[common], help="List current + recent versions.")
    p_versions_show = versions_sub.add_parser("show", parents=[common],
                                              help="Show a version summary (current by default).")
    p_versions_show.add_argument("version_number", nargs="?", default=None,
                                 help="Version number (defaults to the open version).")

    # promote-recover
    p_pr = sub.add_parser("promote-recover", parents=[common],
                          help="Promotion + recovery levers (confirms before mutating).")
    p_pr.add_argument("action", choices=sorted(_PROMOTE_RECOVER_ACTIONS),
                      help="Operator lever to invoke.")
    p_pr.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt (for scripting).")
    p_pr.add_argument("--reason", default=None,
                      help="Reason recorded for cut / clear-deploy-failed.")
    p_pr.add_argument("--force", action="store_true",
                      help="Force clear-deploy-failed when serving SHA is unobservable.")

    # logs
    p_logs = sub.add_parser("logs", parents=[common], help="Read Cloud Run logs via gcloud.")
    logs_sub = p_logs.add_subparsers(dest="logs_cmd")

    def _add_log_common(p):
        p.add_argument("--minutes", type=int, default=None, help="Lookback window in minutes.")
        p.add_argument("--limit", type=int, default=None, help="Max log entries.")
        p.add_argument("--full", action="store_true", help="Show full multi-line entries.")

    p_log_errors = logs_sub.add_parser("errors", parents=[common], help="Recent error logs.")
    _add_log_common(p_log_errors)
    p_log_tail = logs_sub.add_parser("tail", parents=[common], help="Recent logs at a severity.")
    _add_log_common(p_log_tail)
    p_log_tail.add_argument("--severity", default="INFO", help="Minimum severity (default INFO).")
    p_log_search = logs_sub.add_parser("search", parents=[common], help="Search logs for a pattern.")
    p_log_search.add_argument("pattern", help="Substring to match in textPayload.")
    _add_log_common(p_log_search)
    p_log_task = logs_sub.add_parser("task", parents=[common], help="Logs mentioning a task id.")
    p_log_task.add_argument("task_id", help="Task id to grep for.")
    _add_log_common(p_log_task)

    return parser


_DISPATCH = {
    "status": cmd_status,
    "tasks": cmd_tasks,
    "versions": cmd_versions,
    "promote-recover": cmd_promote_recover,
    "logs": cmd_logs,
}


def main(argv: list | None = None) -> int:
    # Windows consoles default to cp1252; force UTF-8 so non-ASCII glyphs never
    # crash output (mirrors scripts/ops.py).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    handler = _DISPATCH[args.command]
    handler(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
