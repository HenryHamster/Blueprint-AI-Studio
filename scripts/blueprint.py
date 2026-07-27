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
    diagnostics       stuck tasks, circuit breakers, grouped failure modes
    tasks list|show   list/filter tasks, or show one by id
    versions list|show  release versions with phase/status/sha and a summary
    trials            capability-trial verdict, arm counts, days-to-decision projection
    fleet pool|warm|accounts|zones  VM account pool, warm pool, and zone health
    promote-recover   promotion + recovery operator levers (confirm before mutate)
    ops               system intervention: repair state, health reset, requeue, restart
    logs tail|errors|search|task   Cloud Logging reads (via gcloud)

Examples:
    python scripts/blueprint.py status
    python scripts/blueprint.py --json status
    python scripts/blueprint.py diagnostics
    python scripts/blueprint.py tasks list --status IN_PROGRESS --type create
    python scripts/blueprint.py tasks show 7ab3f93a
    python scripts/blueprint.py versions show
    python scripts/blueprint.py trials
    python scripts/blueprint.py fleet pool
    python scripts/blueprint.py fleet accounts --disable evan --yes
    python scripts/blueprint.py promote-recover qualify --yes
    python scripts/blueprint.py logs errors --minutes 30

See also:
    scripts/blueprint_runner.py — the separate CLI for local runner management
    (start, check, install, uninstall, update, status).  All runner lifecycle
    operations are in blueprint_runner.py; blueprint.py has no runner
    subcommands.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

from blueprint_api import (
    APIError,
    api,
    get_project_id,
    list_projects,
    load_config,
    resolve_project_id_by_name,
)

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


# ── projects ─────────────────────────────────────────────────────────────────

def cmd_projects(args: argparse.Namespace) -> None:
    """List every project as ``id · name · status``. Respects ``--json``."""
    projects = list_projects()
    _emit(args, {"projects": projects, "total": len(projects)}, _render_projects)


def _render_projects(data: dict) -> None:
    projects = data.get("projects") or []
    print(f"=== PROJECTS ({len(projects)}) ===")
    if not projects:
        print("  (none)")
        return
    for p in projects:
        pid = p.get("id") or "?"
        name = p.get("name") or "?"
        status = p.get("status") or p.get("state") or "-"
        print(f"  {pid} · {name} · {status}")


# ── status ───────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    """Fetch canonical system state from GET /api/admin/state and version-lifecycle."""
    pid = get_project_id(getattr(args, "project_id", None))
    state, state_err = _safe_read(f"/api/admin/state?project_id={pid}")
    state = state or {}
    lifecycle, lifecycle_err = _safe_read(f"/api/admin/projects/{pid}/version-lifecycle")
    lifecycle = lifecycle or {}
    deploy = state.get("deploy") or {}
    version = state.get("version")
    fleet_raw = state.get("fleet") or {}

    serving = (version.get("serving") or {}) if version else {}

    errors = {k: v for k, v in (("state", state_err), ("lifecycle", lifecycle_err)) if v}
    data = {
        "project_id": pid,
        "deploy_state": {
            "status": deploy.get("status"),
            "current_revision": deploy.get("current_revision"),
            "pause_reason": deploy.get("pause_reason"),
            "rollback_reason": deploy.get("rollback_reason"),
            "halted": deploy.get("halted"),
        },
        "current_version": (
            {
                "version_number": version.get("version_number"),
                "phase": version.get("phase"),
                "qualification_state": version.get("qualification_state"),
                "merged_task_count": version.get("merged_task_count"),
                "in_flight_task_count": version.get("in_flight_task_count"),
                "active_candidate_sha": serving.get("sha"),
                "serving_status": serving.get("status"),
                "serving_confirmed_at": serving.get("confirmed_at"),
            }
            if version
            else None
        ),
        "fleet": {
            "active": fleet_raw.get("active"),
            "queued": fleet_raw.get("queued"),
            "evaluating": fleet_raw.get("evaluating"),
            "parked": fleet_raw.get("parked"),
            "stale_heartbeat_count": fleet_raw.get("stale_heartbeat_count"),
        },
        "promotion": {
            "enabled_effective": lifecycle.get("promotion_enabled_effective"),
            "enabled_source": lifecycle.get("promotion_enabled_source"),
            "ready": None,
            "reason": None,
        },
        "errors": errors,
    }
    _emit(args, data, _render_status)


def _render_status(data: dict) -> None:
    ds = data["deploy_state"]
    print("=== DEPLOY STATE ===")
    print(f"  Status:    {ds.get('status', '?')}")
    print(f"  Revision:  {ds.get('current_revision', '?')}")
    if ds.get("halted"):
        print(f"  Halted:    yes")
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
              f"in_flight={cv.get('in_flight_task_count')}  "
              f"candidate={(cv.get('active_candidate_sha') or '-')[:12]}")
        srv_status = cv.get("serving_status")
        srv_confirmed = cv.get("serving_confirmed_at")
        if srv_status is not None or srv_confirmed is not None:
            print(f"  serving_status={srv_status}  confirmed_at={srv_confirmed}")

    fleet = data.get("fleet") or {}
    print("\n=== FLEET ===")
    print(f"  active={fleet.get('active')}  queued={fleet.get('queued')}  "
          f"evaluating={fleet.get('evaluating')}  parked={fleet.get('parked')}  "
          f"stale_hb={fleet.get('stale_heartbeat_count')}")

    errors = data.get("errors") or {}
    if errors:
        print("\n=== READ ERRORS (partial status) ===")
        for endpoint, err in errors.items():
            print(f"  {endpoint}: {err}")


# ── diagnostics ──────────────────────────────────────────────────────────────

def cmd_diagnostics(args: argparse.Namespace) -> None:
    """Pipeline triage: stuck tasks, circuit breakers, grouped failure modes.

    Read-only view over GET /api/admin/diagnostics. Groups the returned stuck
    tasks by failure reason client-side (no extra API call) so the operator can
    see at a glance which failure mode dominates. Uses ``_safe_read`` so a
    diagnostics-endpoint failure degrades to an error dict rather than crashing.
    """
    diag, err = _safe_read("/api/admin/diagnostics")
    diag = diag or {}
    stuck_tasks = diag.get("stuck_tasks") or []
    circuit_breakers = diag.get("circuit_breakers") or {}

    grouped: dict = {}
    for task in stuck_tasks:
        # The live API emits ``stuck_reason``; the documented shape calls it
        # ``reason``. Accept either so grouping works against both.
        reason = task.get("reason") or task.get("stuck_reason") or "unknown"
        grouped.setdefault(reason, []).append(task.get("task_id"))

    data = {
        "stuck_tasks": stuck_tasks,
        "circuit_breakers": circuit_breakers,
        "grouped_failures": grouped,
        "total_stuck": diag.get("total_stuck_count", len(stuck_tasks)),
        "queued_count": diag.get("queued_count"),
        "in_progress_count": diag.get("in_progress_count"),
        "warnings": diag.get("warnings") or [],
        "errors": {"diagnostics": err} if err else {},
    }
    _emit(args, data, _render_diagnostics)


def _render_diagnostics(data: dict) -> None:
    print("=== DIAGNOSTICS ===")
    print(f"  stuck={data.get('total_stuck')}  queued={data.get('queued_count')}  "
          f"in_progress={data.get('in_progress_count')}")

    stuck = data.get("stuck_tasks") or []
    print("\n=== STUCK TASKS ===")
    if not stuck:
        print("  (none)")
    else:
        for s in stuck:
            tid = (s.get("task_id") or "?")[:8]
            title = (s.get("title") or "")[:50]
            reason = s.get("reason") or s.get("stuck_reason") or "?"
            secs = s.get("stuck_for_seconds")
            secs_str = f"  {secs}s" if secs is not None else ""
            print(f"  {tid}  {reason:18} {title}{secs_str}")

    cbs = data.get("circuit_breakers") or {}
    print("\n=== CIRCUIT BREAKERS ===")
    if not cbs:
        print("  (none)")
    else:
        for pid, cb in cbs.items():
            state = (cb or {}).get("state", "?")
            fails = (cb or {}).get("failure_count", 0)
            cooldown = (cb or {}).get("cooldown_ends_at")
            suffix = f"  cooldown_until={cooldown}" if cooldown else ""
            print(f"  {pid[:8]}  state={state}  failures={fails}{suffix}")

    grouped = data.get("grouped_failures") or {}
    print("\n=== GROUPED FAILURES ===")
    if not grouped:
        print("  (none)")
    else:
        for reason, ids in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            sample = ", ".join((i or "?")[:8] for i in ids)
            print(f"  {reason:18} {len(ids)}  ({sample})")

    errors = data.get("errors") or {}
    if errors:
        print("\n=== READ ERRORS (diagnostics unavailable) ===")
        for endpoint, e in errors.items():
            print(f"  {endpoint}: {e}")


# ── tasks ──────────────────────────────────────────────────────────────────--

def cmd_tasks(args: argparse.Namespace) -> None:
    """Dispatch task subcommands: list / show / cancel / requeue / scope / tree / failures /
    evaluating / cancel-all / requeue-ci / trigger-rollup / complete / set-status /
    reset-iteration-cap / archive / research / autonomy-debug."""
    sub = getattr(args, "tasks_cmd", None) or "list"
    if sub == "show":
        _tasks_show(args)
    elif sub == "cancel":
        _tasks_cancel(args)
    elif sub == "requeue":
        _tasks_requeue(args)
    elif sub == "scope":
        _tasks_scope(args)
    elif sub == "tree":
        _tasks_tree(args)
    elif sub == "failures":
        _tasks_failures(args)
    elif sub == "evaluating":
        _tasks_evaluating(args)
    elif sub == "cancel-all":
        _tasks_cancel_all(args)
    elif sub == "requeue-ci":
        _tasks_requeue_ci(args)
    elif sub == "trigger-rollup":
        _tasks_trigger_rollup(args)
    elif sub == "complete":
        _tasks_complete(args)
    elif sub == "set-status":
        _tasks_set_status(args)
    elif sub == "reset-iteration-cap":
        _tasks_reset_iteration_cap(args)
    elif sub == "archive":
        _tasks_archive(args)
    elif sub == "research":
        _tasks_research(args)
    elif sub == "autonomy-debug":
        _tasks_autonomy_debug(args)
    elif sub == "create":
        _tasks_create(args)
    else:
        _tasks_list(args)


# Valid failure_class values the backend's classify_failure can emit. Used as
# argparse choices for `tasks list --failure-class` and to drive the client-side
# filter. Only FAILED/CANCELLED tasks ever carry a failure_class.
FAILURE_CLASSES = frozenset({
    "CODE_ERROR", "PROVISION_FAILURE", "AGENT_TIMEOUT", "EXTERNAL_API",
    "RATE_LIMIT", "SCOPE_OVERFLOW", "MIGRATION_ERROR", "VIEWPORT_REGRESSION",
    "agent_stalled", "BLOCKED_PRECONDITION", "SCOPE_EMPTY",
    "park_wake_timeout", "park_wake_stranded",
})

# Statuses whose tasks can carry a failure_class (backend only classifies
# FAILED/CANCELLED). A --failure-class combined with any other status filter
# can never match, so we warn.
_FAILURE_CLASS_STATUSES = frozenset({"FAILED", "CANCELLED"})


def _tasks_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    failure_classes = set(getattr(args, "failure_class", None) or [])
    # When client-side failure-class filtering is active, fetch a wider page so
    # the post-fetch narrowing has enough rows to work with (mirrors the web
    # console, which also filters failure_class client-side).
    display_limit = getattr(args, "limit", 50)
    fetch_limit = max(display_limit, 500) if failure_classes else display_limit
    path = f"/api/admin/tasks?project_id={pid}&limit={fetch_limit}"
    # --status and --blocked both contribute repeatable status= params; the
    # backend accepts a List[TaskStatus], so BLOCKED appends rather than replaces.
    requested_statuses = []
    if getattr(args, "status", None):
        path += f"&status={args.status}"
        requested_statuses.append(str(args.status).upper())
    if getattr(args, "blocked", False):
        path += "&status=BLOCKED"
        requested_statuses.append("BLOCKED")
    if getattr(args, "approval_needed", False):
        path += "&approval_needed=true"
    if getattr(args, "offset", 0):
        path += f"&offset={args.offset}"
    if getattr(args, "task_type", None):
        path += f"&task_type={args.task_type}"
    if getattr(args, "direction", None):
        path += f"&direction_id={args.direction}"
    if getattr(args, "search", None):
        from urllib.parse import quote

        path += f"&search={quote(args.search)}"

    # A failure-class filter only matches FAILED/CANCELLED tasks; warn (do not
    # hard-error) when paired with a status that can never carry a failure_class.
    if failure_classes:
        impossible = [s for s in requested_statuses
                      if s not in _FAILURE_CLASS_STATUSES]
        if impossible:
            print(
                "WARNING: --failure-class only matches FAILED/CANCELLED tasks; "
                f"status filter(s) {', '.join(impossible)} can never carry a "
                "failure_class, so results may be empty.",
                file=sys.stderr,
            )

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
    payload = {"tasks": tasks, "total": total}
    if failure_classes:
        # Client-side narrowing to the requested failure classes, then re-trim
        # to the display limit the operator asked for.
        filtered = [t for t in (tasks or [])
                    if t.get("failure_class") in failure_classes]
        payload["tasks"] = filtered[:display_limit]
        payload["filtered_client_side"] = True
    _emit(args, payload, _render_task_list)


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


def _tasks_cancel(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'cancel' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not _confirm(f"Cancel task {args.task_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("PATCH", f"/api/admin/tasks/{args.task_id}", {"status": "CANCELLED"})
    _emit(args, result, lambda d: print(f"  {args.task_id}: {d.get('status', 'ok')}"))


def _tasks_requeue(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'requeue' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not _confirm(f"Requeue task {args.task_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body = {"status": "QUEUED"}
    guidance = getattr(args, "requeue_prompt_suffix", None)
    if guidance is not None:
        guidance = guidance.strip()
        if not guidance:
            print("ERROR: --prompt-suffix/--guidance cannot be empty", file=sys.stderr)
            sys.exit(2)
        body["requeue_prompt_suffix"] = guidance
    result = api("PATCH", f"/api/admin/tasks/{args.task_id}", body)
    _emit(args, result, lambda d: print(f"  {args.task_id}: {d.get('status', 'ok')}"))


def _tasks_show(args: argparse.Namespace) -> None:
    task = api("GET", f"/api/admin/tasks/{args.task_id}")
    _emit(args, task, _render_task_detail)


def _tasks_scope(args: argparse.Namespace) -> None:
    task = api("GET", f"/api/admin/tasks/{args.task_id}")
    verbose = getattr(args, "verbose", False)
    payload = first_payload(task)
    prompt = payload.get("prompt", "")
    ctx = task.get("context") or {}
    data = {
        "id": task.get("id"),
        "title": task.get("title"),
        "task_type": task.get("task_type"),
        "status": task.get("status"),
        "prompt": prompt,
        "prompt_length": len(prompt),
        "research_question": ctx.get("research_question"),
        "scope": ctx.get("scope"),
    }
    _emit(args, data, lambda d: _render_task_scope(d, verbose))


def _render_task_scope(d: dict, verbose: bool) -> None:
    tid = (d.get("id") or "?")[:8]
    print(f"=== {tid} [{d.get('task_type', '?')}] {d.get('status', '?')} ===")
    print(f"  Title: {d.get('title', '?')}")
    rq = d.get("research_question")
    if rq:
        print(f"\n  Research Question:\n    {rq}")
    scope = d.get("scope")
    if scope:
        print(f"\n  Scope: {scope}")
    prompt = d.get("prompt", "")
    if prompt:
        limit = len(prompt) if verbose else 500
        print(f"\n  Prompt ({d.get('prompt_length')} chars):")
        print(f"    {prompt[:limit]}")
        if not verbose and len(prompt) > limit:
            print(f"    ... ({len(prompt) - limit} more chars, use -v for full)")


def _tasks_tree(args: argparse.Namespace) -> None:
    parent = api("GET", f"/api/admin/tasks/{args.parent_id}")
    child_ids = parent.get("child_task_ids") or []
    show_all = getattr(args, "all", False)
    children = []
    for cid in child_ids:
        try:
            child = api("GET", f"/api/admin/tasks/{cid}")
            children.append(child)
        except Exception:  # noqa: BLE001
            children.append({"id": cid, "status": "UNKNOWN", "title": "(fetch failed)"})

    if not show_all:
        children = [
            c for c in children
            if first_payload(c).get("type") not in ("review", "fix", "debug")
            and c.get("task_type") not in ("review", "fix", "debug")
        ]

    data = {"parent": parent, "children": children}
    _emit(args, data, _render_task_tree)


def _render_task_tree(data: dict) -> None:
    p = data["parent"]
    pid = (p.get("id") or "?")[:8]
    print(f"=== TREE {pid} ===")
    print(f"  [{p.get('status', '?'):13}] {pid}  {p.get('title', '?')[:70]}")
    children = data["children"]
    if not children:
        print("  (no children)")
        return
    icons = {"QUEUED": "Q", "PROVISIONING": "P", "IN_PROGRESS": "R",
             "COMPLETED": "D", "FAILED": "F", "CANCELLED": "X",
             "EVALUATING": "E", "UNKNOWN": "?"}
    print(f"\n  Children ({len(children)}):")
    for c in children:
        cid = (c.get("id") or "?")[:8]
        status = c.get("status", "?")
        icon = icons.get(status, "?")
        merged = (c.get("context") or {}).get("pr_merged", False)
        pr = first_payload(c).get("pr_number", "-")
        title = (c.get("title") or "")[:55]
        print(f"    [{icon}] {cid}  merged={str(merged):<5}  pr={pr:<4}  {title}")


def _tasks_failures(args: argparse.Namespace) -> None:
    """Fetch FAILED tasks, group by root cause, and emit triage-ready output."""
    pid = get_project_id(getattr(args, "project_id", None))
    limit = getattr(args, "limit", 50)

    tasks: list = []
    offset = 0
    while len(tasks) < limit:
        path = (f"/api/admin/tasks?project_id={pid}&status=FAILED"
                f"&limit={min(500, limit - len(tasks))}&offset={offset}")
        resp = api("GET", path)
        batch = resp.get("tasks", resp) if isinstance(resp, dict) else resp
        if not batch:
            break
        tasks.extend(batch)
        offset += len(batch)
        if len(batch) < 500:
            break

    buckets: dict = {}
    for t in tasks:
        msg = t.get("latest_status_message") or ""
        error = t.get("error_logs") or ""
        ctx = t.get("context") or {}
        fc = ctx.get("failure_class", "")

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
            cause = "AGENT_STARTUP_FAILURE"
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

    data = {
        "total": len(tasks),
        "buckets": {
            cause: [
                {
                    "id": t.get("id"),
                    "task_type": t.get("task_type"),
                    "title": t.get("title"),
                    "attempt_count": t.get("attempt_count", 0),
                    "parent_task_id": t.get("parent_task_id"),
                    "latest_status_message": t.get("latest_status_message"),
                    "error_logs": t.get("error_logs"),
                }
                for t in group
            ]
            for cause, group in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        },
    }
    _emit(args, data, _render_failures)


def _safe_trunc(s: str, n: int) -> str:
    return (s or "")[:n].encode("ascii", "replace").decode()


def _render_failures(data: dict) -> None:
    total = data.get("total", 0)
    buckets = data.get("buckets") or {}
    if not total:
        print("No failed tasks.")
        return
    print(f"\n=== FAILED TASKS ({total}) ===\n")
    for cause, group in buckets.items():
        print(f"--- {cause} ({len(group)} tasks) ---")
        for t in group:
            tid = (t.get("id") or "?")[:8]
            ttype = t.get("task_type") or "?"
            title = _safe_trunc(t.get("title") or "", 55)
            attempts = t.get("attempt_count", 0)
            parent = (t.get("parent_task_id") or "")[:8]
            msg = _safe_trunc(t.get("latest_status_message") or "", 100)
            error = _safe_trunc((t.get("error_logs") or "").split("\n")[0], 120)
            print(f"  {tid}  [{ttype:8}]  att={attempts}  parent={parent or '-':>8}  {title}")
            if error:
                print(f"    ERR: {error}")
            elif msg:
                print(f"    MSG: {msg}")
        print()
    print("=== SUMMARY ===")
    for cause, group in buckets.items():
        requeueable = [t for t in group if cause not in ("CODE_ERROR",)]
        suffix = f" ({len(requeueable)} requeueable)" if requeueable else ""
        print(f"  {cause}: {len(group)} tasks{suffix}")


def _tasks_evaluating(args: argparse.Namespace) -> None:
    """List EVALUATING tasks with CI/PR status and latest message."""
    pid = get_project_id(getattr(args, "project_id", None))
    resp = api("GET", f"/api/admin/tasks?project_id={pid}&status=EVALUATING&limit=200")
    tasks = resp.get("tasks", resp) if isinstance(resp, dict) else resp
    _emit(args, {"tasks": tasks, "total": len(tasks)}, _render_evaluating_list)


def _render_evaluating_list(data: dict) -> None:
    tasks = data.get("tasks") or []
    print(f"=== EVALUATING TASKS ({len(tasks)}) ===")
    if not tasks:
        print("  (none)")
        return
    for t in tasks:
        tid = (t.get("id") or "?")[:8]
        loop = (t.get("context") or {}).get("review_loop") or {}
        ci = loop.get("ci_waiting", False)
        pr = loop.get("pr_number", "-")
        ttype = t.get("task_type", "?")
        title = (t.get("title") or "")[:55]
        msg = (t.get("latest_status_message") or "")[:70]
        print(f"  {tid}  [{ttype:8}]  ci={str(ci):<5}  pr={pr:<4}  {title}")
        if msg:
            print(f"           {msg}")


def _tasks_autonomy_debug(args: argparse.Namespace) -> None:
    """Fetch autonomy-debug for a project: blocking guards, eligible directions."""
    pid = get_project_id(getattr(args, "project_id", None))
    data = api("GET", f"/api/admin/projects/{pid}/autonomy-debug")
    _emit(args, data, _render_autonomy_debug)


def _render_autonomy_debug(data: dict) -> None:
    print("=== AUTONOMY DEBUG ===")
    blocked = data.get("spawn_blocked")
    if blocked is True:
        print("  can_spawn: NO (blocked)")
    elif blocked is False:
        print("  can_spawn: YES — no guards; next tick will spawn")
    else:
        print("  can_spawn: ?")
    guards = data.get("blocking_guards") or []
    if guards:
        print(f"\n--- Blocking Guards ({len(guards)}) ---")
        for g in guards:
            print(f"  {g.get('guard', '?')}: {g.get('reason', '?')}")
    dirs = data.get("directions") or []
    eligible = [d for d in dirs if d.get("is_eligible")]
    if dirs:
        print(f"\n--- Directions ({len(eligible)}/{len(dirs)} eligible) ---")
        for d in dirs:
            did = (d.get("id") or "?")[:10]
            text = (d.get("text") or "?")[:70]
            markers = []
            if d.get("seconds_until_eligible"):
                markers.append(f"cooldown {d['seconds_until_eligible'] // 60}m")
            if d.get("is_eligible"):
                markers.append("eligible")
            tag = f" [{', '.join(markers)}]" if markers else ""
            print(f"  {did}  {text}{tag}")


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
    ctx = t.get("context") or {}
    fc = ctx.get("failure_category") or ctx.get("failure_class")
    if fc and t.get("status") in ("FAILED", "CANCELLED"):
        print(f"  FailClass: {fc}")
    diag = ctx.get("compute_terminal_diagnostics")
    if diag:
        reason = diag.get("terminal_reason", "")
        phase = diag.get("vm_phase", "")
        phase_label = f" [{phase}]" if phase else ""
        print(f"  ComputeDiag:{phase_label} {reason}"[:200])
        cause = diag.get("provisioning_cause")
        if cause:
            print(f"    ProvCause: {cause}"[:200])
    desc = t.get("description")
    if desc:
        print(f"\n  Description:\n    {desc[:500]}")


# ── tasks: bulk and manual-state extensions ──────────────────────────────────

def _require_write_config(action: str) -> None:
    """Exit 1 with a clear error when the resolved config is read-only."""
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            f"ERROR: '{action}' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)


def _tasks_cancel_all(args: argparse.Namespace) -> None:
    _require_write_config("cancel-all")
    pid = get_project_id(getattr(args, "project_id", None))
    status_filter = getattr(args, "status", None)
    category_filter = getattr(args, "category", None)
    statuses = ([status_filter] if status_filter
                else ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"])
    to_cancel = []
    for status in statuses:
        offset = 0
        while True:
            resp = api("GET",
                       f"/api/admin/tasks?project_id={pid}&status={status}"
                       f"&limit=500&offset={offset}")
            batch = resp.get("tasks", resp) if isinstance(resp, dict) else resp
            if not batch:
                break
            for t in batch:
                if (category_filter
                        and (t.get("context") or {}).get("failure_category") != category_filter):
                    continue
                to_cancel.append(t)
            offset += len(batch)
            if len(batch) < 500:
                break
    if not to_cancel:
        print("No matching tasks to cancel.")
        return
    if not _confirm(f"Cancel {len(to_cancel)} task(s)?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    cancelled = 0
    for t in to_cancel:
        try:
            api("PATCH", f"/api/admin/tasks/{t['id']}", {"status": "CANCELLED"})
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  {t['id'][:8]}: FAILED — {exc}")
    suffix = f" (category={category_filter})" if category_filter else ""
    print(f"Cancelled {cancelled}/{len(to_cancel)} tasks{suffix}.")


def _tasks_requeue_ci(args: argparse.Namespace) -> None:
    _require_write_config("requeue-ci")
    pid = get_project_id(getattr(args, "project_id", None))
    resp = api("GET", f"/api/admin/tasks?project_id={pid}&status=EVALUATING&limit=100")
    tasks = resp.get("tasks", resp) if isinstance(resp, dict) else resp
    ci_tasks = [t for t in tasks
                if (t.get("context") or {}).get("review_loop", {}).get("ci_waiting")]
    if not ci_tasks:
        print("No CI-waiting EVALUATING tasks.")
        return
    if not _confirm(f"Requeue {len(ci_tasks)} CI-waiting task(s)?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    for t in ci_tasks:
        pr = (t.get("context") or {}).get("review_loop", {}).get("pr_number", "?")
        try:
            api("PATCH", f"/api/admin/tasks/{t['id']}", {"status": "QUEUED"})
            print(f"  {t['id'][:8]}  (PR #{pr}): requeued")
        except Exception as exc:  # noqa: BLE001
            print(f"  {t['id'][:8]}: FAILED — {exc}")


def _tasks_trigger_rollup(args: argparse.Namespace) -> None:
    _require_write_config("trigger-rollup")
    parent_id = args.parent_id
    if not _confirm(f"Force rollup for task {parent_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", f"/api/admin/tasks/{parent_id}/rollup")
    pr_num = result.get("rollup_pr_number") or result.get("pr_number")
    pr_url = result.get("rollup_pr_url") or result.get("pr_url") or ""
    _emit(args, result, lambda r: print(f"  Rollup PR created: #{pr_num} {pr_url}"))


def _tasks_complete(args: argparse.Namespace) -> None:
    _require_write_config("complete")
    if not _confirm(f"Mark task {args.task_id} COMPLETED?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body: dict = {"status": "COMPLETED"}
    if getattr(args, "message", None):
        body["latest_status_message"] = args.message
    result = api("PATCH", f"/api/admin/tasks/{args.task_id}", body)
    _emit(args, result, lambda r: print(f"  {args.task_id}: marked COMPLETED"))


def _tasks_set_status(args: argparse.Namespace) -> None:
    _require_write_config("set-status")
    if not _confirm(f"Set task {args.task_id} to {args.new_status}?",
                    getattr(args, "yes", False)):
        print("Aborted.")
        return
    body: dict = {"status": args.new_status}
    if getattr(args, "message", None):
        body["latest_status_message"] = args.message
    result = api("PATCH", f"/api/admin/tasks/{args.task_id}", body)
    _emit(args, result, lambda r: print(f"  {args.task_id}: -> {args.new_status}"))


def _tasks_reset_iteration_cap(args: argparse.Namespace) -> None:
    _require_write_config("reset-iteration-cap")
    if not _confirm(f"Reset iteration cap for task {args.task_id}?",
                    getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", f"/api/admin/tasks/{args.task_id}/reset-iteration-cap")
    _emit(args, result, lambda r: print(
        f"  PR #{r.get('pr_number')}: archived {r.get('archived_count')} failed review/fix tasks, "
        f"per-chain iteration {r.get('previous_per_chain_iteration')}->0"
        + (" (cleared requires_approval)" if r.get("requires_approval_cleared") else "")
    ))


def _tasks_archive(args: argparse.Namespace) -> None:
    _require_write_config("archive")
    pipelines = getattr(args, "pipelines", False)
    completed_only = getattr(args, "completed", False)
    filter_type = "completed" if completed_only else "terminal"
    label = "all fully-terminal pipelines" if pipelines else f"all {filter_type} tasks"
    if not _confirm(f"Archive {label}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body: dict = {"filter": "terminal"} if pipelines else {"filter": filter_type}
    result = api("POST", "/api/admin/tasks/bulk-archive", body)
    _emit(args, result, lambda r: print(f"Archived {r.get('archived_count', '?')} tasks."))


def _tasks_research(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    task_id = getattr(args, "task_id", None)
    limit = getattr(args, "limit", 5)
    if task_id:
        task = api("GET", f"/api/admin/tasks/{task_id}")
        _emit(args, task, _render_task_detail)
        return
    resp = api("GET",
               f"/api/admin/tasks?project_id={pid}&status=COMPLETED"
               f"&task_type=research&limit={limit}")
    tasks = resp.get("tasks", resp) if isinstance(resp, dict) else resp
    if not tasks:
        print("No recent research tasks found.")
        return
    _emit(args, {"tasks": tasks, "total": len(tasks)}, _render_task_list)


def _tasks_create(args: argparse.Namespace) -> None:
    _require_write_config("tasks create")
    cfg = load_config()
    title = args.title
    prompt = args.prompt
    task_type = getattr(args, "task_type", "create")
    github_repo = getattr(args, "repo", None) or cfg.get("default_repo", "")
    base_branch = getattr(args, "base_branch", None) or cfg.get("default_base_branch", "main")
    if not github_repo:
        print("ERROR: --repo is required (or set default_repo in config).", file=sys.stderr)
        sys.exit(1)
    if not _confirm(f"Create task: {title[:60]}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body: dict = {
        "github_repo": github_repo,
        "title": title,
        "prompt": prompt,
        "task_type": task_type,
        "base_branch": base_branch,
    }
    if getattr(args, "model", None):
        body["model"] = args.model
    if getattr(args, "effort", None):
        body["effort"] = args.effort
    if getattr(args, "hotfix", False):
        body["hotfix"] = True
    result = api("POST", "/api/admin/tasks", body)
    _emit(args, result, lambda r: print(
        f"  task created: {r.get('id', '?')}  [{r.get('task_type', '?')}]  {r.get('title', '')}"
    ))


# ── versions ──────────────────────────────────────────────────────────────--

def cmd_versions(args: argparse.Namespace) -> None:
    """Dispatch ``versions list`` / ``versions show`` (defaults to list)."""
    pid = get_project_id(getattr(args, "project_id", None))

    # Full-history browsing (``versions list --all`` or ``--limit/--offset``) goes
    # through the paginated release-versions endpoint rather than the cached
    # version-lifecycle read. Default ``versions list`` keeps using lifecycle below
    # so the no-flag UX is unchanged.
    sub = getattr(args, "versions_cmd", None) or "list"
    if sub == "list" and (
        getattr(args, "all_versions", False)
        or getattr(args, "limit", None) is not None
    ):
        _versions_list_paginated(args, pid)
        return

    lifecycle = api("GET", f"/api/admin/projects/{pid}/version-lifecycle")
    cv = lifecycle.get("current_version")
    recent = lifecycle.get("recent_versions") or []

    if sub == "export-changelog":
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
        if not ver and target is not None:
            ver_summary, _ = _safe_read(
                f"/api/admin/projects/{pid}/release-versions/{target}/summary"
            )
            if ver_summary:
                ver = ver_summary
        if not ver:
            msg = (f"version {target} not found"
                   if target is not None else "no open version")
            print(msg, file=sys.stderr)
            return
        ver_num = ver.get("version_number")
        if ver is cv:
            tasks_merged = cv.get("tasks_merged") or []
        else:
            ver_id = ver.get("id")
            if ver_id:
                ver_summary, _ = _safe_read(
                    f"/api/admin/release-versions/{ver_id}/summary"
                )
                tasks_merged = (ver_summary or {}).get("tasks_merged") or []
            else:
                tasks_merged = ver.get("tasks_merged") or []
        heading = f"What shipped in v{ver_num}"
        md = _changelog_to_markdown(tasks_merged, heading=heading)
        out_path = getattr(args, "out", None)
        if out_path:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(md)
            print(f"Wrote changelog to {out_path}")
        else:
            print(md, end="")
        return

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
        if not ver and target is not None:
            # Fallback: fetch from the historical summary endpoint if not in recent list.
            ver_summary, _ = _safe_read(
                f"/api/admin/projects/{pid}/release-versions/{target}/summary"
            )
            if ver_summary:
                ver = ver_summary
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
            "merged_task_count": ver.get("merged_task_count") or ver.get("merged_count"),
            "qualification_state": ver.get("qualification_state"),
            "acceptance_aggregate": ver.get("acceptance_aggregate"),
            "goal": ver.get("goal"),
        }
        if ver is cv:
            # Current (open) version: tasks_merged is already in the lifecycle response.
            summary["tasks_merged"] = cv.get("tasks_merged") or []
            summary["aggregate_serving_status"] = cv.get("aggregate_serving_status")
        else:
            # Closed version: fetch the rich summary if a UUID is available.
            ver_id = ver.get("id")
            if ver_id:
                ver_summary, _ = _safe_read(
                    f"/api/admin/release-versions/{ver_id}/summary"
                )
                if ver_summary:
                    summary["tasks_merged"] = ver_summary.get("tasks_merged") or []
                    summary["highlights"] = ver_summary.get("highlights")
                    summary["serving_status"] = ver_summary.get("serving_status")
                    # Populate with rich data from the summary endpoint.
                    if ver_summary.get("phase") is not None:
                        summary["phase"] = ver_summary["phase"]
                    if ver_summary.get("goal") is not None:
                        summary["goal"] = ver_summary["goal"]
                    if ver_summary.get("merged_count") is not None:
                        summary["merged_task_count"] = ver_summary["merged_count"]
                    if ver_summary.get("acceptance_aggregate") is not None:
                        summary["acceptance_aggregate"] = ver_summary["acceptance_aggregate"]
                else:
                    summary["tasks_merged"] = []
            else:
                # Fallback endpoint response already has tasks_merged and highlights
                summary["tasks_merged"] = ver.get("tasks_merged") or []
                summary["highlights"] = ver.get("highlights")
                summary["serving_status"] = ver.get("serving_status")
        _emit(args, summary, _render_version_summary)
        return

    data = {
        "current_version": cv,
        "recent_versions": recent,
        "promotion_enabled_effective": lifecycle.get("promotion_enabled_effective"),
    }
    _emit(args, data, _render_version_list)


def _version_headline(v: dict) -> str:
    """Derive the human-readable phase-context headline for a version row.

    Mirrors the backend summary endpoint's headline derivation
    (admin_release_version.py ~L1566) from fields already present in the
    version-lifecycle response, so ``versions list`` shows the same
    at-a-glance phrasing the UI does instead of bare numeric counts.
    """
    status = v.get("status") or "open"
    phase = v.get("phase") or "?"
    merged = v.get("merged_task_count")
    if merged is None:
        merged = v.get("merged_count")
    serving_raw = v.get("aggregate_serving_status") or v.get("serving_status") or ""
    is_serving = str(serving_raw).lower() in ("serving_confirmed", "serving")
    if status == "promoted" and is_serving:
        return "promoted & serving"
    if status == "promoted":
        return "promoted"
    if status == "abandoned":
        return "abandoned"
    if not merged:
        return f"open in {phase} with no merged work yet"
    return f"open in {phase} with {merged} merged"


def _version_line(v: dict, marker: str = " ") -> str:
    num = v.get("version_number", "?")
    phase = v.get("phase", "?")
    status = v.get("status", "open")
    sha = (v.get("promoted_sha") or v.get("served_sha") or "-")[:12]
    merged = v.get("merged_task_count") or v.get("merged_count")
    merged_str = f"  merged={merged}" if merged is not None else ""
    tasks_merged = v.get("tasks_merged") or []
    tasks_str = f"  tasks={len(tasks_merged)}" if tasks_merged else ""
    # Prefer the server-computed headline when present; fall back to local derivation.
    server_highlights = v.get("highlights")
    if isinstance(server_highlights, dict) and server_highlights.get("headline"):
        headline = server_highlights["headline"]
    else:
        headline = _version_headline(v)
    return (f"  {marker} v{num}  [{status:9}] phase={phase:9} sha={sha}"
            f"{merged_str}{tasks_str}  — {headline}")


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


def _versions_list_paginated(args: argparse.Namespace, pid: str) -> None:
    """Browse the full version history via the paginated release-versions endpoint.

    ``--all`` walks every page (fixed internal page size); ``--limit/--offset``
    fetch a single explicit window. Either way the rendered output shows the
    pre-pagination ``total`` and the window currently in view.
    """
    base = f"/api/admin/projects/{pid}/release-versions"
    offset = getattr(args, "offset", 0) or 0

    if getattr(args, "all_versions", False):
        page_size = 100
        versions: list = []
        total = 0
        cursor = offset
        while True:
            resp, err = _safe_read(f"{base}?limit={page_size}&offset={cursor}")
            if err or resp is None:
                print(err or "no response from release-versions", file=sys.stderr)
                return
            total = resp.get("total", 0)
            batch = resp.get("versions") or []
            versions.extend(batch)
            cursor += len(batch)
            # Stop on a short page or once we've covered the reported total.
            if not batch or len(batch) < page_size or cursor >= total:
                break
        data = {
            "versions": versions,
            "total": total,
            "limit": None,
            "offset": offset,
        }
    else:
        limit = getattr(args, "limit", None) or 20
        resp, err = _safe_read(f"{base}?limit={limit}&offset={offset}")
        if err or resp is None:
            print(err or "no response from release-versions", file=sys.stderr)
            return
        data = {
            "versions": resp.get("versions") or [],
            "total": resp.get("total", 0),
            "limit": resp.get("limit", limit),
            "offset": resp.get("offset", offset),
        }

    _emit(args, data, _render_versions_paginated)


def _render_versions_paginated(data: dict) -> None:
    total = data.get("total", 0)
    versions = data.get("versions") or []
    print(f"=== RELEASE VERSIONS ({total} total) ===")
    if not versions:
        print("  (no versions in this window)")
        return
    for v in versions:
        print(_version_line(v))
    offset = data.get("offset", 0) or 0
    end = offset + len(versions) - 1
    print(f"\n  (showing {offset}-{end} of {total})")


def _changelog_to_markdown(tasks_merged: list, heading: str | None = None) -> str:
    """Format a tasks_merged list as a Markdown release-notes block.

    Groups entries by task_type (sorted alphabetically) and emits a
    ``## heading`` (when provided) followed by ``### type (N)`` sections
    with ``- #PR title`` bullet lines.
    """
    if not tasks_merged:
        return "No tasks merged into this version.\n"
    lines: list = []
    if heading:
        lines.append(f"## {heading}\n")
    by_type: dict = {}
    for t in tasks_merged:
        ttype = t.get("task_type") or "other"
        by_type.setdefault(ttype, []).append(t)
    for ttype in sorted(by_type):
        group = by_type[ttype]
        lines.append(f"### {ttype} ({len(group)})")
        for item in group:
            pr = item.get("pr_number")
            title = (item.get("title") or "").strip()
            pr_str = f"#{pr} " if pr else ""
            lines.append(f"- {pr_str}{title}")
        lines.append("")
    return "\n".join(lines)


def _render_version_summary(s: dict) -> None:
    print(f"=== VERSION v{s.get('version_number')} SUMMARY ===")
    print(f"  phase:        {s.get('phase')}")
    print(f"  status:       {s.get('status')}")
    print(f"  sha:          {(s.get('promoted_sha') or '-')[:12]}")
    print(f"  merged_tasks: {s.get('merged_task_count')}")
    print(f"  qual_state:   {s.get('qualification_state')}")
    serving = s.get("aggregate_serving_status") or s.get("serving_status")
    if serving:
        print(f"  serving:      {serving}")
    agg = s.get("acceptance_aggregate")
    if agg:
        print(f"  acceptance:   {json.dumps(agg)}")
    if s.get("goal"):
        print(f"  goal:         {s['goal'][:200]}")
    highlights = s.get("highlights") or {}
    headline = highlights.get("headline") if isinstance(highlights, dict) else None
    narrative = highlights.get("narrative") if isinstance(highlights, dict) else None
    if headline:
        print(f"\n  Summary: {headline}")
    if narrative:
        print(f"  {narrative}")
    tasks_merged = s.get("tasks_merged") or []
    if tasks_merged:
        print(f"\n  Changelog ({len(tasks_merged)} tasks):")
        by_type: dict = {}
        for t in tasks_merged:
            ttype = t.get("task_type") or "other"
            by_type.setdefault(ttype, []).append(t)
        for ttype in sorted(by_type):
            print(f"    [{ttype}]")
            for item in by_type[ttype]:
                pr = item.get("pr_number")
                title = (item.get("title") or "")[:70]
                pr_str = f"  #{pr}" if pr else ""
                print(f"      {title}{pr_str}")


# ── system ────────────────────────────────────────────────────────────────────

def cmd_system(args: argparse.Namespace) -> None:
    """Break-glass system commands: repair, restart, zones, autonomy."""
    sub = getattr(args, "system_cmd", None)
    dispatch = {
        "repair": _system_repair,
        "restart": _system_restart,
        "zones": _system_zones,
        "autonomy": _system_autonomy,
    }
    if sub not in dispatch:
        print("system: subcommand required. Run `blueprint system --help`.", file=sys.stderr)
        sys.exit(1)
    dispatch[sub](args)


def _system_repair(args: argparse.Namespace) -> None:
    if load_config().get("read_only"):
        print("ERROR: 'repair' requires an admin API key.", file=sys.stderr)
        sys.exit(1)
    if not _confirm("Trigger repair-state (fix stuck/orphaned tasks)?",
                    getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", "/api/admin/repair-state", {})
    repairs = result.get("repairs", [])
    def _render_repair(d: dict) -> None:
        print(f"Repairs: {d.get('count', 0)}")
        for r in d.get("repairs", []):
            print(f"  - {r}")
        if not d.get("repairs"):
            print("  (no repairs needed)")
    _emit(args, result, _render_repair)


def _system_restart(args: argparse.Namespace) -> None:
    if load_config().get("read_only"):
        print("ERROR: 'restart' requires an admin API key.", file=sys.stderr)
        sys.exit(1)
    if not _confirm("Restart orchestrator background loops?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", "/api/admin/restart-orchestrator", {})
    _emit(args, result, lambda d: print(f"Restart: {json.dumps(d, indent=2, default=str)}"))


def _system_zones(args: argparse.Namespace) -> None:
    zones, err = _safe_read("/api/admin/zone-health")
    _emit(args, {"zones": zones or {}, "error": err}, _render_zones)


def _render_zones(data: dict) -> None:
    print("=== ZONE HEALTH ===")
    if data.get("error"):
        print(f"  error: {data['error']}")
        return
    zones = data.get("zones") or {}
    if isinstance(zones, dict):
        for zone, info in sorted(zones.items()):
            i = info or {}
            status = i.get("status", "?")
            fails = i.get("consecutive_failures", 0)
            last = i.get("last_success", "?")
            print(f"  {zone:30}: {status:12} fails={fails} last_ok={last}")
    else:
        print(f"  {zones}")


def _system_autonomy(args: argparse.Namespace) -> None:
    data, err = _safe_read("/api/admin/autonomous-status")
    result = {"projects": (data or {}).get("projects", []), "error": err}
    _emit(args, result, _render_autonomy)


def _render_autonomy(data: dict) -> None:
    print("=== AUTONOMY ===")
    if data.get("error"):
        print(f"  error: {data['error']}")
        return
    projects = data.get("projects") or []
    if not projects:
        print("  (none)")
        return
    for p in projects:
        print(f"  {p.get('project_name', '?')}: "
              f"enabled={p.get('autonomous_enabled')}  "
              f"failures={p.get('consecutive_failures', 0)}  "
              f"last_research={p.get('last_research_at', 'never')}")


# ── monitor ───────────────────────────────────────────────────────────────────

def cmd_monitor(args: argparse.Namespace) -> None:
    """Show system alerts, digest, and activity feed for situational awareness."""
    sub = getattr(args, "monitor_cmd", None)
    dispatch = {
        "alerts": _monitor_alerts,
        "digest": _monitor_digest,
        "activity": _monitor_activity,
    }
    if sub not in dispatch:
        print("monitor: subcommand required. Run `blueprint monitor --help`.", file=sys.stderr)
        sys.exit(1)
    dispatch[sub](args)


def _monitor_alerts(args: argparse.Namespace) -> None:
    alerts, err = _safe_read("/api/admin/alerts")
    result = {"alerts": alerts or [], "error": err}
    _emit(args, result, _render_alerts)


def _render_alerts(data: dict) -> None:
    print("=== ALERTS ===")
    if data.get("error"):
        print(f"  error: {data['error']}")
        return
    alerts = data.get("alerts") or []
    if not alerts:
        print("  (no active alerts)")
        return
    for a in alerts:
        severity = a.get("severity", "?")
        title = a.get("title", "?")
        msg = (a.get("message") or "")[:120]
        ts = (a.get("created_at") or "")[:19]
        aid = a.get("id", "?")[:8]
        tid = (a.get("task_id") or "")[:8]
        task_ref = f"  task={tid}" if tid else ""
        print(f"  [{severity:8}] {aid}  {ts}  {title}{task_ref}")
        if msg:
            print(f"             {msg}")


def _monitor_digest(args: argparse.Namespace) -> None:
    hours = getattr(args, "hours", 24)
    data, err = _safe_read(f"/api/admin/digest?hours={hours}")
    result = {"digest": data or {}, "hours": hours, "error": err}
    _emit(args, result, _render_digest)


def _render_digest(data: dict) -> None:
    hours = data.get("hours", "?")
    print(f"=== DIGEST (last {hours}h) ===")
    if data.get("error"):
        print(f"  error: {data['error']}")
        return
    d = data.get("digest") or {}
    print(f"  Completed:        {d.get('completed_count', '?')}")
    print(f"  Failed:           {d.get('failed_count', '?')}")
    print(f"  PRs opened:       {d.get('prs_opened', '?')}")
    print(f"  Research cycles:  {d.get('research_cycles', '?')}")
    print(f"  Pending approval: {d.get('pending_approval_count', '?')}")
    print(f"  Active now:       {d.get('active_count', '?')} ({d.get('running_count', '?')} running, {d.get('queued_count', '?')} queued)")


def _monitor_activity(args: argparse.Namespace) -> None:
    limit = getattr(args, "limit", 20)
    resp, err = _safe_read(f"/api/admin/activity?limit={limit}")
    # Handle both list and dict responses (endpoint may evolve)
    events = (resp.get("events", []) if isinstance(resp, dict) else resp) if resp else []
    result = {"events": events, "limit": limit, "error": err}
    _emit(args, result, _render_activity)


def _render_activity(data: dict) -> None:
    print(f"=== ACTIVITY (last {len(data.get('events', []))} events) ===")
    if data.get("error"):
        print(f"  error: {data['error']}")
        return
    events = data.get("events") or []
    if not events:
        print("  (no recent activity)")
        return
    icons = {
        "TASK_COMPLETED": "done",
        "TASK_FAILED": "FAIL",
        "RESEARCH_FINDINGS": "research",
        "DEPLOY": "deploy",
    }
    for e in events:
        etype = e.get("type", "?")
        icon = icons.get(etype, etype)
        ts = (e.get("timestamp") or "")[:16]
        title = (e.get("title") or "?")[:55]
        tid = (e.get("task_id") or "")[:8]
        pr = e.get("pr_url") or ""
        err = (e.get("error_summary") or "")[:80]
        dur = e.get("duration_seconds")
        dur_str = f"  {int(dur)}s" if dur else ""
        pr_str = f"  {pr}" if pr else ""
        print(f"  {ts}  [{icon:10}]  {tid}  {title}{dur_str}{pr_str}")
        if err:
            print(f"                              {err}")


# ── directions ───────────────────────────────────────────────────────────────

def cmd_directions(args: argparse.Namespace) -> None:
    """Dispatch directions subcommands: list / show / add / update / unhold."""
    sub = getattr(args, "directions_cmd", None) or "list"
    if sub == "show":
        _directions_show(args)
    elif sub == "add":
        _directions_add(args)
    elif sub == "update":
        _directions_update(args)
    elif sub == "retarget":
        _directions_retarget(args)
    elif sub == "activate":
        _directions_activate(args)
    elif sub == "cancel":
        _directions_cancel(args)
    elif sub == "unhold":
        _directions_unhold(args)
    else:
        _directions_list(args)


def _directions_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    resp = api("GET", f"/api/admin/projects/{pid}/directions")
    directions = resp.get("directions", []) if isinstance(resp, dict) else resp

    if getattr(args, "staged", False):
        directions = [d for d in directions if d.get("status") == "staged"]
    elif not getattr(args, "all", False):
        directions = [d for d in directions
                      if d.get("status") in ("active", "proposed", "staged")]

    _emit(args, {"directions": directions}, _render_directions_list)


def _staged_direction_marker(d: dict) -> str | None:
    if d.get("status") != "staged":
        return None
    target = d.get("staged_for_version")
    return f"staged→v{target}" if target is not None else "staged (unpinned)"


def _render_directions_list(data: dict) -> None:
    directions = data.get("directions") or []
    print(f"=== DIRECTIONS ({len(directions)}) ===")
    if not directions:
        print("  (none)")
        return
    for d in directions:
        did = (d.get("id") or "?")
        status = d.get("status", "active")
        prio = d.get("priority", "?")
        text = (d.get("text") or "")[:100]
        history = d.get("research_history") or []
        notes = d.get("notes") or []
        held = d.get("held", False)
        staged_marker = _staged_direction_marker(d)
        flags = []
        if d.get("possibly_complete"):
            flags.append("possibly_complete")
        if d.get("possibly_saturated"):
            flags.append("saturated")
        if held:
            flags.append("held")
        if d.get("pending_update"):
            flags.append("pending_update")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        staged_str = f"  {staged_marker}" if staged_marker else ""
        print(f"  {did[:12]}  [{prio:6}] {status:8}  {text}{staged_str}{flag_str}"
              f"  ({len(history)} cycles, {len(notes)} notes)")


def _resolve_direction(args: argparse.Namespace, direction_id: str, *, require_staged: bool = False) -> dict:
    pid = get_project_id(getattr(args, "project_id", None))
    resp = api("GET", f"/api/admin/projects/{pid}/directions")
    directions = resp.get("directions", []) if isinstance(resp, dict) else resp
    matches = [d for d in directions if (d.get("id") or "").startswith(direction_id)]
    if not matches:
        print(f"Direction not found: {direction_id}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"Direction prefix is ambiguous: {direction_id}", file=sys.stderr)
        sys.exit(1)
    direction = matches[0]
    if require_staged and direction.get("status") != "staged":
        print(f"Direction is not staged: {direction.get('id', direction_id)}", file=sys.stderr)
        sys.exit(1)
    return direction


def _directions_show(args: argparse.Namespace) -> None:
    d = _resolve_direction(args, args.direction_id)
    _emit(args, d, _render_direction_detail)


def _render_direction_detail(d: dict) -> None:
    print(f"=== DIRECTION: {d.get('id', '?')} ===")
    print(f"  Text:       {d.get('text', '?')}")
    print(f"  Status:     {d.get('status', '?')}")
    print(f"  Priority:   {d.get('priority', '?')}")
    print(f"  Category:   {d.get('category', '-')}")
    print(f"  Added:      {(d.get('added_at') or '?')[:19]}")
    if d.get("held"):
        print(f"  Held:       yes  reason={d.get('held_reason', '?')}")
    if d.get("status") == "paused" or d.get("paused_reason"):
        print(f"  Paused why: {d.get('paused_reason', '(none recorded)')}")
        print(f"  Paused at:  {(d.get('paused_at') or '?')[:19]}")
    if d.get("status") == "staged":
        staged_for_version = d.get("staged_for_version")
        staged_for = f"v{staged_for_version}" if staged_for_version is not None else "unpinned"
        print(f"  Staged for: {staged_for}")
        print(f"  Staged at:  {(d.get('staged_at') or '?')[:19]}")
        print(f"  Waiting age: {_age_str(d.get('staged_at'))}")
    if d.get("needs_manual_intervention"):
        print(f"  ** NEEDS MANUAL INTERVENTION "
              f"(since {(d.get('needs_manual_intervention_at') or '?')[:19]})")
    if d.get("paths"):
        print(f"  Paths:      {', '.join(d['paths'])}")
    if d.get("completion_criteria"):
        print(f"  Criteria:   {d['completion_criteria']}")
    if d.get("possibly_complete"):
        print(f"  ** Possibly complete: {d.get('completion_suggestion', '(no suggestion)')}")
    if d.get("possibly_saturated"):
        print(f"  ** Possibly saturated")

    notes = d.get("notes") or []
    if notes:
        print(f"\n  --- Notes ({len(notes)}) ---")
        for n in notes:
            ts = (n.get("added_at") or n.get("timestamp") or "?")[:16]
            text = (n.get("text") or "?")[:200]
            print(f"    [{ts}] {text}")

    history = d.get("research_history") or []
    agg = d.get("history_aggregate") or {}
    if history or agg:
        total_cycles = len(history) + int(agg.get("total_cycles") or 0)
        total_merged = (sum(e.get("prs_merged", 0) for e in history)
                        + int(agg.get("total_prs_merged") or 0))
        total_tasks = (sum(e.get("tasks_created", 0) for e in history)
                       + int(agg.get("total_tasks_created") or 0))
        print(f"\n  --- Research History ({total_cycles} cycles, "
              f"{total_tasks} tasks, {total_merged} merged) ---")
        for h in history[-5:]:
            ts = (h.get("researched_at") or "?")[:16]
            tc = h.get("tasks_created", 0)
            pm = h.get("prs_merged", 0)
            print(f"    {ts}  tasks={tc}  merged={pm}")

    ctx = d.get("research_context", "")
    if ctx:
        print(f"\n  --- Research Context ---")
        lines = ctx.strip().split("\n")
        for line in lines[:20]:
            print(f"    {line}")
        if len(lines) > 20:
            print(f"    ... ({len(lines) - 20} more lines)")


def _directions_add(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'add' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)
    text = args.text
    staged = getattr(args, "staged", False)
    priority = getattr(args, "priority", "medium") or "medium"
    paths = getattr(args, "paths", None) or []
    completion_criteria = getattr(args, "completion_criteria", None)
    body: dict = {"text": text, "priority": priority}
    if paths:
        body["paths"] = paths
    if completion_criteria:
        body["completion_criteria"] = completion_criteria
    if staged:
        body["staged"] = True
    if not _confirm(f"Add {'staged ' if staged else ''}direction: {text[:60]}?",
                    getattr(args, "yes", False)):
        print("Aborted.")
        return
    pid = get_project_id(getattr(args, "project_id", None))
    result = api("POST", f"/api/admin/projects/{pid}/directions", body)
    d = result.get("direction", {})
    _emit(args, result, lambda r: print(
        f"Added {d.get('id', '?')} [{priority}]{' (staged)' if staged else ''}: {text[:80]}"
    ))


def _directions_update(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'update' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)

    pid = get_project_id(getattr(args, "project_id", None))
    d = _resolve_direction(args, args.direction_id,
                           require_staged=getattr(args, "staged_for_version", None) is not None)
    did = d["id"]
    reset_all = getattr(args, "reset_all", False)
    staged_for_version = getattr(args, "staged_for_version", None)

    patch: dict = {}
    if getattr(args, "text", None):
        patch["text"] = args.text
    if getattr(args, "priority", None):
        patch["priority"] = args.priority
    if getattr(args, "status", None):
        patch["status"] = args.status
    if staged_for_version is not None:
        patch["staged_for_version"] = staged_for_version
    if getattr(args, "reset_context", False) or reset_all:
        patch["research_context"] = (
            "## Established Findings\n\n## Failed Approaches\n\n## Open Questions\n"
        )
    if getattr(args, "reset_history", False) or reset_all:
        patch["research_history"] = []
    if getattr(args, "reset_agent_notes", False) or reset_all:
        # Filter out agent-added notes from the resolved direction.
        notes = (d.get("notes") or [])
        patch["notes"] = [n for n in notes if not n.get("added_by_task")]

    if not patch:
        print("Nothing to update. Use --text, --priority, --status, --staged-for-version,"
              " or a reset flag.",
              file=sys.stderr)
        sys.exit(1)

    is_retarget = list(patch.keys()) == ["staged_for_version"]
    if not _confirm(
        f"Retarget staged direction {did} to v{staged_for_version}?"
        if is_retarget else f"Update direction {did}?",
        getattr(args, "yes", False),
    ):
        print("Aborted.")
        return

    result = api("PATCH", f"/api/admin/projects/{pid}/directions/{did}", patch)
    _emit(args, result, lambda r: print(
        f"Retargeted {did} to v{staged_for_version}"
        if is_retarget else f"Updated {did}: {', '.join(patch.keys())}"
    ))


def _directions_retarget(args: argparse.Namespace) -> None:
    _require_write_config("directions retarget")
    pid = get_project_id(getattr(args, "project_id", None))
    d = _resolve_direction(args, args.direction_id, require_staged=True)
    did = d["id"]
    version = args.version
    if not _confirm(f"Retarget staged direction {did} to v{version}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("PATCH", f"/api/admin/projects/{pid}/directions/{did}", {
        "staged_for_version": version,
    })
    _emit(args, result, lambda r: print(f"Retargeted {did} to v{version}"))


def _directions_activate(args: argparse.Namespace) -> None:
    _require_write_config("directions activate")
    pid = get_project_id(getattr(args, "project_id", None))
    d = _resolve_direction(args, args.direction_id, require_staged=True)
    did = d["id"]
    if not _confirm(f"Activate staged direction {did} now?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("PATCH", f"/api/admin/projects/{pid}/directions/{did}", {
        "status": "active",
    })
    _emit(args, result, lambda r: print(f"Activated staged direction {did}"))


def _directions_cancel(args: argparse.Namespace) -> None:
    _require_write_config("directions cancel")
    pid = get_project_id(getattr(args, "project_id", None))
    d = _resolve_direction(args, args.direction_id, require_staged=True)
    did = d["id"]
    if not _confirm(f"Cancel staged direction {did}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("DELETE", f"/api/admin/projects/{pid}/directions/{did}")
    _emit(args, result, lambda r: print(
        f"Cancelled staged direction {did}: {r.get('message', 'removed')}"
    ))


def _directions_unhold(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'unhold' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
            )
        sys.exit(1)
    pid = get_project_id(getattr(args, "project_id", None))
    d = _resolve_direction(args, args.direction_id)
    did = d["id"]
    if not _confirm(f"Clear hold on direction {did}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    pid = get_project_id(getattr(args, "project_id", None))
    result = api("POST", f"/api/admin/projects/{pid}/directions/{did}/unhold")
    was_held = result.get("was_held", False)
    _emit(args, result, lambda r: print(
        f"Unheld {did} (was_held={was_held})"
        if was_held else f"Direction {did} was not held (idempotent)"
    ))


# ── trials ───────────────────────────────────────────────────────────────────

def cmd_trials(args: argparse.Namespace) -> None:
    """List capability-trial verdicts with convergence projection and arm composition."""
    pid = get_project_id(getattr(args, "project_id", None))
    digest, err = _safe_read(f"/api/admin/digest?project_id={pid}")
    digest = digest or {}

    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return

    verdicts = digest.get("capability_adoption_verdicts") or {}
    data = {
        "trials": [
            {
                "cap_name": cap,
                "verdict": v.get("verdict"),
                "lift": v.get("lift"),
                "arm_composition": v.get("arm_composition") or {},
                "accrual_projection": v.get("accrual_projection") or {},
                "containment_rate": v.get("containment_rate"),
                "delivery_rate": v.get("delivery_rate"),
            }
            for cap, v in verdicts.items()
        ],
        "errors": {"digest": err} if err else {},
    }
    _emit(args, data, _render_trials)


def _render_trials(data: dict) -> None:
    trials = data.get("trials") or []
    print("=== CAPABILITY TRIALS ===")
    if not trials:
        print("  (none)")
        return
    for trial in trials:
        cap = trial.get("cap_name", "?")
        verdict = trial.get("verdict", "?")
        lift = trial.get("lift")
        arm_comp = trial.get("arm_composition") or {}
        with_n = arm_comp.get("with_assigned", 0)
        without_n = arm_comp.get("without_assigned", 0)
        accrual = trial.get("accrual_projection") or {}
        days_to_decision = accrual.get("days_to_min_n")

        print(f"\n  {cap}  verdict={verdict}  lift={lift}")
        print(f"    with_assigned={with_n}  without_assigned={without_n}")
        if days_to_decision is not None:
            print(f"    days_to_decision={days_to_decision:.1f}")
        alt_proj = accrual.get("alternative_projections") or {}
        if alt_proj:
            holdback = accrual.get("holdback_fraction", 0)
            print(f"    holdback_fraction={holdback:.2f}  alternative_projections={alt_proj}")


# ── settings ─────────────────────────────────────────────────────────────────

def cmd_settings(args: argparse.Namespace) -> None:
    """Dispatch settings subcommands: show (default) / set."""
    sub = getattr(args, "settings_cmd", None) or "show"
    if sub == "set":
        _settings_set(args)
    else:
        _settings_show(args)


def _settings_show(args: argparse.Namespace) -> None:
    data, err = _safe_read("/api/admin/settings")
    if err or data is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return
    _emit(args, data, _render_settings)


def _settings_set(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'settings set' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)

    updates: dict = {}
    for kv in args.assignments:
        if "=" not in kv:
            print(f"ERROR: invalid KEY=VALUE format: {kv!r}", file=sys.stderr)
            sys.exit(1)
        k, v = kv.split("=", 1)
        if v.lower() in ("true", "false"):
            v = v.lower() == "true"
        else:
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
        updates[k] = v

    if not _confirm(
        f"Update {len(updates)} setting(s): {', '.join(updates.keys())}?",
        getattr(args, "yes", False),
    ):
        print("Aborted.")
        return

    result = api("PATCH", "/api/admin/settings", updates)
    _emit(args, result, _render_settings)


def _render_settings(data: dict) -> None:
    print("=== RUNTIME SETTINGS ===")
    for k, v in sorted(data.items()):
        print(f"  {k}: {v}")


# ── envelope ─────────────────────────────────────────────────────────────────

def cmd_envelope(args: argparse.Namespace) -> None:
    """Dispatch envelope subcommands: show / set."""
    sub = getattr(args, "envelope_cmd", None) or "show"
    if sub == "set":
        _envelope_set(args)
    else:
        _envelope_show(args)


def _envelope_show(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    data, err = _safe_read(f"/api/admin/projects/{pid}/resource-envelope")
    if err or data is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return
    _emit(args, data, _render_envelope)


def _envelope_set(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'envelope set' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)

    updates: dict = {}
    for kv in args.assignments:
        if "=" not in kv:
            print(f"ERROR: invalid KEY=VALUE format: {kv!r}", file=sys.stderr)
            sys.exit(1)
        k, v = kv.split("=", 1)
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass
        updates[k] = v

    if not updates:
        print("ERROR: at least one KEY=VALUE assignment required.", file=sys.stderr)
        sys.exit(1)

    pid = get_project_id(getattr(args, "project_id", None))
    if not _confirm(
        f"Update envelope for project {pid}: {', '.join(f'{k}={v}' for k, v in updates.items())}?",
        getattr(args, "yes", False),
    ):
        print("Aborted.")
        return

    result = api("PATCH", f"/api/admin/projects/{pid}/resource-envelope", updates)
    _emit(args, result, _render_envelope)


def _render_envelope(data: dict) -> None:
    pid = data.get("project_id", "?")
    print(f"=== RESOURCE ENVELOPE (project {pid}) ===")
    print(f"  max_concurrent_vms:           {data.get('max_concurrent_vms')}")
    print(f"  spend_ceiling_usd:            {data.get('spend_ceiling_usd')}")
    print(f"  spend_window_days:            {data.get('spend_window_days')}")
    print(f"  spend_warning_fraction:       {data.get('spend_warning_fraction')}")
    print(f"  max_concurrent_compute_jobs:  {data.get('max_concurrent_compute_jobs')}")
    print(f"  active_vm_count:              {data.get('active_vm_count')}")
    recent = data.get("recent_spend_usd")
    if recent is not None:
        print(f"  recent_spend_usd:             ${float(recent):.4f}")
    throttled = data.get("is_throttled")
    if throttled is not None:
        print(f"  is_throttled:                 {throttled}")


# ── messages ─────────────────────────────────────────────────────────────────

def cmd_messages(args: argparse.Namespace) -> None:
    """Dispatch messages subcommands: list / send / answer / confirm."""
    sub = getattr(args, "messages_cmd", None) or "list"
    if sub == "send":
        _messages_send(args)
    elif sub == "answer":
        _messages_answer(args)
    elif sub == "confirm":
        _messages_confirm(args)
    else:
        _messages_list(args)


def _messages_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    status = getattr(args, "status", "open")
    url = f"/api/admin/projects/{pid}/messages"
    if status:
        url += f"?status={status}"
    msgs = api("GET", url)
    _emit(args, {"messages": msgs}, _render_messages)


_MSG_ICONS = {"BUG_REPORT": "BUG", "FEEDBACK": "FB", "NOTE": "NOTE",
              "QUESTION": "Q", "BLOCKER": "BLOCK"}


def _render_messages(data: dict) -> None:
    msgs = data.get("messages") or []
    if not msgs:
        print("No messages.")
        return
    for m in msgs:
        msg_id = m.get("id", "?")
        icon = _MSG_ICONS.get(m.get("type", ""), "?")
        src = m.get("from", "?")
        age = (m.get("created_at") or "?")[:16]
        print(f"  {msg_id} [{icon}] ({src}, {age}) {(m.get('content') or '')[:120]}")


def _messages_send(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'send' is a mutating action but the resolved config is "
            "read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)
    pid = get_project_id(getattr(args, "project_id", None))
    result = api("POST", f"/api/admin/projects/{pid}/messages", {
        "content": args.content,
        "message_type": getattr(args, "type", "FEEDBACK"),
    })
    _emit(args, result, lambda d: print(f"  Message sent: {d.get('id', '?')}"))


def _messages_answer(args: argparse.Namespace) -> None:
    _require_write_config("answer")
    message_id = args.message_id
    result = api("PATCH", f"/api/admin/messages/{message_id}", {
        "resolution": args.content,
    })
    _emit(args, result, lambda d: print(f"  Answer sent: {d.get('id', '?')}"))


def _messages_confirm(args: argparse.Namespace) -> None:
    """Dispatch messages confirm subcommands: list / accept / dismiss."""
    sub = getattr(args, "confirm_cmd", None) or "list"
    if sub == "accept":
        _messages_confirm_accept(args)
    elif sub == "dismiss":
        _messages_confirm_dismiss(args)
    else:
        _messages_confirm_list(args)


def _messages_confirm_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    project = api("GET", f"/api/admin/projects/{pid}")
    pm = project.get("problem_model") or {}
    items = pm.get("pending_user_confirmation") or []
    _emit(args, {"pending": items}, _render_pending_list)


def _render_pending_list(data: dict) -> None:
    items = data.get("pending") or []
    print(f"=== PENDING CONFIRMATIONS ({len(items)}) ===")
    if not items:
        print("  (none)")
        return
    for p in items:
        pid = p.get("id", "?")
        ptype = p.get("type", "?")
        proposal = (p.get("proposal") or "")[:100]
        rationale = (p.get("rationale") or "")[:80]
        created = (p.get("created_at") or "?")[:16]
        print(f"  {pid}  [{ptype}]  {created}")
        print(f"    {proposal}")
        if rationale:
            print(f"    rationale: {rationale}")


def _messages_confirm_accept(args: argparse.Namespace) -> None:
    _require_write_config("messages confirm accept")
    pid = get_project_id(getattr(args, "project_id", None))
    pending_id = args.pending_id
    if not _confirm(f"Accept pending confirmation {pending_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body: dict = {}
    if getattr(args, "answer", None):
        body["answer"] = args.answer
    result = api("POST", f"/api/admin/projects/{pid}/pending-confirm/{pending_id}/accept", body)
    _emit(args, result, lambda d: print(f"  Accepted: {pending_id}  status={d.get('status', 'ok')}"))


def _messages_confirm_dismiss(args: argparse.Namespace) -> None:
    _require_write_config("messages confirm dismiss")
    pid = get_project_id(getattr(args, "project_id", None))
    pending_id = args.pending_id
    if not _confirm(f"Dismiss pending confirmation {pending_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body: dict = {}
    if getattr(args, "note", None):
        body["note"] = args.note
    result = api("POST", f"/api/admin/projects/{pid}/pending-confirm/{pending_id}/dismiss", body)
    _emit(args, result, lambda d: print(f"  Dismissed: {pending_id}  status={d.get('status', 'ok')}"))


# ── fleet (pool / warm / accounts / zones) ────────────────────────────────────

def cmd_fleet(args: argparse.Namespace) -> None:
    """Dispatch fleet subcommands: pool / warm / accounts / zones."""
    sub = getattr(args, "fleet_cmd", None) or "pool"
    if sub == "warm":
        _fleet_warm(args)
    elif sub == "accounts":
        _fleet_accounts(args)
    elif sub == "zones":
        _fleet_zones(args)
    else:
        _fleet_pool(args)


def _fleet_pool(args: argparse.Namespace) -> None:
    data, err = _safe_read("/api/admin/settings")
    if err:
        print(f"Pool: error ({err})")
        return
    accounts = (data or {}).get("max_plan_accounts", [])
    _emit(args, {"accounts": accounts}, _render_fleet_pool)


def _render_fleet_pool(data: dict) -> None:
    accounts = data.get("accounts") or []
    print("=== MAX PLAN POOL ===")
    print(f"  Total:     {len(accounts)}")
    print(f"  Enabled:   {sum(1 for a in accounts if a.get('enabled', True))}")
    for a in accounts:
        name = a.get("name", a.get("id", "?")[:8])
        status = "OK" if a.get("enabled", True) else "DISABLED"
        rl = a.get("rl_hit_count", 0)
        suffix = f" (rate-limited x{rl})" if rl else ""
        print(f"  {name:20}: active={a.get('active_tasks', 0)} {status}{suffix}")
        for model, info in (a.get("disabled_models") or {}).items():
            at = (info.get("at") or "")[:19]
            print(f"    {model}: BLOCKED (hit #{info.get('hits', 0)}, since {at})")


def _fleet_warm(args: argparse.Namespace) -> None:
    ov, err = _safe_read("/api/admin/warm-vm-state")
    if err:
        print(f"Warm pool: error ({err})")
        return
    _emit(args, ov or {}, _render_fleet_warm)


def _render_fleet_warm(data: dict) -> None:
    pm = data.get("pool_metrics") or {}
    wpm = data.get("warm_pin_metrics") or {}
    if not pm:
        print("=== WARM VM POOL ===\n  (pool_metrics empty — pool disabled or backend error)")
        return
    print("=== WARM VM POOL ===")
    print(f"  enabled:        {pm.get('enabled')}")
    allow = pm.get("project_allowlist") or []
    print(f"  scope:          {allow if allow else 'fleet-wide (no allowlist)'}")
    print(f"  target_size:    {pm.get('target_size')}   idle_ttl_secs: {pm.get('idle_ttl_secs')}")
    print(f"  current_size:   {pm.get('current_size')}   idle_now: {pm.get('idle_count')}")
    print(f"  idle_cost_usd:  projected={pm.get('projected_idle_cost_usd')} "
          f"settled={pm.get('settled_idle_cost_usd')} live={pm.get('live_idle_cost_usd')}")

    print("\n=== CLAIM CIRCUIT BREAKER ===")
    sup_until = wpm.get("pool_suppressed_until")
    print(f"  suppressed:     {wpm.get('pool_circuit_breaker_suppressed')}"
          + (f" until {sup_until}" if sup_until else ""))
    print(f"  failures/1h:    {wpm.get('pool_claim_failures_last_hour')} "
          f"(threshold {wpm.get('pool_failure_rate_threshold')})")
    print(f"  active pool-origin claims: {wpm.get('active_pool_origin_claims')}  "
          f"pool_origin_share: {wpm.get('pool_origin_share')}")

    cg = pm.get("cost_guard")
    if cg is not None:
        print("\n=== COST GUARD ===")
        print(f"  engaged:        {cg.get('engaged')}"
              + (f"  reason={cg.get('reason')}" if cg.get("reason") else ""))
        print(f"  eligible_rate:  {cg.get('hit_rate')}  (min {cg.get('min_hit_rate')})  "
              f"[raw {cg.get('raw_hit_rate')}]")
        print(f"  eligible_disp:  {cg.get('total_dispatches')}  "
              f"[raw {cg.get('raw_total_dispatches')}]")
        print(f"  idle_cost_usd:  {cg.get('idle_cost_usd')}  (min {cg.get('min_idle_cost_usd')})")

    funnel = pm.get("funnel") or {}
    print(f"\n=== DISPATCH FUNNEL (last {funnel.get('window_hours', '?')}h) ===")
    er = funnel.get("eligible_hit_rate")
    rr = funnel.get("pool_hit_rate")
    print(f"  eligible_rate:  {er if er is not None else 'n/a (no eligible demand)'}"
          f"  ({funnel.get('pool_lease_hits')} hits / {funnel.get('eligible_dispatches')} eligible)")
    print(f"  raw_rate:       {rr if rr is not None else 'n/a (no dispatches)'}"
          f"  ({funnel.get('pool_lease_hits')} hits / {funnel.get('total_dispatches')} total)")
    print(f"  cold:           {funnel.get('cold_dispatches')}")
    misses = funnel.get("lease_miss_by_reason") or {}
    print(f"  lease misses:   {funnel.get('lease_miss_total')} "
          f"(eligible {funnel.get('eligible_miss_total')})")
    for reason, n in sorted(misses.items(), key=lambda kv: -kv[1]):
        print(f"    - {reason:22}: {n}")
    by_acct = funnel.get("by_account") or {}
    if by_acct:
        print("\n=== PER-ACCOUNT ===")
        for acct, b in sorted(by_acct.items(), key=lambda kv: -(kv[1].get("total_dispatches") or 0)):
            ehr = b.get("eligible_hit_rate")
            print(f"  {acct:36} elig_rate={ehr if ehr is not None else 'n/a':<8} "
                  f"raw={b.get('pool_hit_rate')} hits={b.get('pool_lease_hits')} "
                  f"cold={b.get('cold_dispatches')} misses={b.get('lease_miss_total')}")
            for reason, n in sorted((b.get("lease_miss_by_reason") or {}).items(), key=lambda kv: -kv[1]):
                print(f"      - {reason}: {n}")


def _fleet_resolve_account(query: str) -> dict | None:
    """Resolve a Max Plan account by name (case-insensitive substring) or ID prefix."""
    s = api("GET", "/api/admin/settings")
    q = query.lower()
    for a in s.get("max_plan_accounts", []):
        if a["id"].lower().startswith(q) or q in a.get("name", "").lower():
            return a
    return None


def _fleet_accounts(args: argparse.Namespace) -> None:
    disable = getattr(args, "disable", None)
    enable = getattr(args, "enable", None)
    remove = getattr(args, "remove", None)

    if disable or enable:
        cfg = load_config()
        if cfg.get("read_only"):
            print("ERROR: mutating action requires an admin API key.", file=sys.stderr)
            sys.exit(1)
        target = disable or enable
        is_enable = bool(enable)
        verb = "Enable" if is_enable else "Disable"
        if not _confirm(f"{verb} account {target}?", getattr(args, "yes", False)):
            print("Aborted.")
            return
        if target.lower() == "all":
            s = api("GET", "/api/admin/settings")
            for a in s.get("max_plan_accounts", []):
                if a.get("enabled", True) != is_enable:
                    result = api("PATCH", f"/api/admin/max-plan-accounts/{a['id']}", {"enabled": is_enable})
                    print(f"  {verb}d: {result.get('name', a['id'][:8])}")
                else:
                    state = "enabled" if is_enable else "disabled"
                    print(f"  {a.get('name', a['id'][:8])}: already {state}")
            return
        acct = _fleet_resolve_account(target)
        if not acct:
            print(f"Account not found: {target}")
            return
        result = api("PATCH", f"/api/admin/max-plan-accounts/{acct['id']}", {"enabled": is_enable})
        print(f"{verb}d: {result.get('name', acct['id'][:8])}")
        return

    if remove:
        cfg = load_config()
        if cfg.get("read_only"):
            print("ERROR: mutating action requires an admin API key.", file=sys.stderr)
            sys.exit(1)
        if not _confirm(f"Remove account {remove}?", getattr(args, "yes", False)):
            print("Aborted.")
            return
        acct = _fleet_resolve_account(remove)
        if not acct:
            print(f"Account not found: {remove}")
            return
        path = f"/api/admin/max-plan-accounts/{acct['id']}"
        if getattr(args, "force", False):
            path += "?force=true"
        result = api("DELETE", path)
        print(f"Removed: {result.get('message', 'done')}")
        return

    _fleet_pool(args)


def _fleet_zones(args: argparse.Namespace) -> None:
    zones, err = _safe_read("/api/admin/zone-health")
    if err:
        print(f"Zones: error ({err})")
        return
    _emit(args, {"zones": zones or {}}, _render_fleet_zones)


def _render_fleet_zones(data: dict) -> None:
    zones = data.get("zones") or {}
    print("=== ZONE HEALTH ===")
    if isinstance(zones, dict):
        for zone, zdata in sorted(zones.items()):
            zdata = zdata or {}
            status = zdata.get("status", "?")
            last = zdata.get("last_success", "?")
            fails = zdata.get("consecutive_failures", 0)
            print(f"  {zone:30}: {status:12} fails={fails} last_ok={last}")
    else:
        print(f"  {zones}")


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


def _staged_activated_by_cut(staged: list, version_number: int) -> list:
    """Return staged directions that a cut of ``version_number`` will activate."""
    return [
        s for s in (staged or [])
        if s and (s.get("staged_for_version") is None or s.get("staged_for_version") <= version_number)
    ]


def _cut_confirmation_prompt(pid: str) -> str:
    """Build the version-cut confirmation prompt from lifecycle state.

    Falls back to the generic confirmation when the lifecycle read is
    unavailable or too incomplete to safely describe consequences.
    """
    lifecycle, err = _safe_read(f"/api/admin/projects/{pid}/version-lifecycle")
    if err:
        return "About to abandon the open version and open the next. Proceed?"

    lifecycle = lifecycle or {}
    cv = lifecycle.get("current_version") or {}
    version_number = cv.get("version_number")
    if version_number is None:
        return "About to abandon the open version and open the next. Proceed?"

    staged = lifecycle.get("staged_directions") or []
    activating = _staged_activated_by_cut(staged, version_number)
    in_flight = cv.get("in_flight_task_count")
    qual_state_raw = cv.get("qualification_state")
    if isinstance(qual_state_raw, dict):
        qual_state = qual_state_raw
        qual_state_name = qual_state.get("state")
        promotion_pr_number = qual_state.get("promotion_pr_number")
    else:
        qual_state = {}
        qual_state_name = qual_state_raw if isinstance(qual_state_raw, str) else None
        promotion_pr_number = None

    lines = [f"About to abandon v{version_number} and open the next version.", "This cut will:"]
    if activating:
        lines.append(
            f"- Start {len(activating)} staged direction{'s' if len(activating) != 1 else ''} immediately "
            "(rather than waiting for a clean promotion):"
        )
        for direction in activating:
            text = direction.get("text") or "(no description)"
            lines.append(f"  - {text}")
    else:
        lines.append("- No staged directions will activate.")

    if in_flight is None:
        lines.append("- In-flight task count unavailable.")
    elif in_flight > 0:
        lines.append(
            f"- {in_flight} in-flight task{'s' if in_flight != 1 else ''} will NOT be stopped - "
            "they keep running and land against the new version."
        )
    else:
        lines.append("- No tasks are currently in flight.")

    if qual_state_name == "promotion_in_flight":
        pr_label = f" #{promotion_pr_number}" if promotion_pr_number else ""
        lines.append(
            f"- Promotion PR{pr_label} is open and auto-merging - this cut will be rejected until it "
            "lands or is canceled."
        )
    elif qual_state_name == "promotion_check_wait":
        pr_label = f" #{promotion_pr_number}" if promotion_pr_number else ""
        lines.append(
            f"- Migration-carrying promotion PR{pr_label} is open and waiting for required checks - "
            "Blueprint retries the merge automatically; this cut will be rejected until it lands or is canceled."
        )

    lines.append("Proceed?")
    return "\n".join(lines)


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

    prompt = _cut_confirmation_prompt(pid) if action == "cut" else f"About to {summary}. Proceed?"
    if not _confirm(prompt, getattr(args, "yes", False)):
        print("Aborted.")
        return

    result = api(method, path, body if body else {})
    _emit(args, {"action": action, "result": result}, _render_promote_recover)


def _render_promote_recover(data: dict) -> None:
    print(f"=== {data['action']} ===")
    print(json.dumps(data.get("result"), indent=2, default=str))


# ── ops (system intervention) ────────────────────────────────────────────────

def cmd_ops(args: argparse.Namespace) -> None:
    """Dispatch ops subcommands: repair / reset-health / requeue-failed / restart-orchestrator / circuit-breaker."""
    sub = getattr(args, "ops_cmd", None)
    if sub == "repair":
        _ops_repair(args)
    elif sub == "reset-health":
        _ops_reset_health(args)
    elif sub == "requeue-failed":
        _ops_requeue_failed(args)
    elif sub == "restart-orchestrator":
        _ops_restart_orchestrator(args)
    elif sub == "circuit-breaker":
        _ops_circuit_breaker(args)
    else:
        print("ERROR: ops requires a subcommand", file=sys.stderr)
        sys.exit(1)


def _ops_repair(args: argparse.Namespace) -> None:
    """Repair stuck state."""
    _require_write_config("repair")
    if not _confirm("Repair stuck/orphaned tasks?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", "/api/admin/repair-state", {})
    repairs = result.get('repairs', [])
    data = {"action": "repair", "count": result.get('count', 0), "repairs": repairs}
    def _render(d):
        print(f"Repairs: {d.get('count')}")
        if d.get('repairs'):
            for r in d.get('repairs', []):
                print(f"  - {r}")
        else:
            print("  (no repairs needed)")
    _emit(args, data, _render)


def _ops_reset_health(args: argparse.Namespace) -> None:
    """Force-reset health checks."""
    _require_write_config("reset-health")
    if not _confirm("Force-reset health checks?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", "/api/admin/deploy-state/reset-health", {})
    _emit(args, result, lambda d: print(json.dumps(d, indent=2, default=str)))


def _ops_requeue_failed(args: argparse.Namespace) -> None:
    """Requeue all FAILED tasks."""
    _require_write_config("requeue-failed")
    from blueprint_api import list_tasks
    failed = list_tasks(status="FAILED", limit=100)
    if not failed:
        _emit(args, {"count": 0}, lambda d: print("No failed tasks."))
        return
    if not _confirm(f"Requeue {len(failed)} failed task(s)?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    requeued = []
    failed_requeue = []
    for t in failed:
        tid = t['id']
        try:
            api("PATCH", f"/api/admin/tasks/{tid}", {"status": "QUEUED"})
            requeued.append(tid)
        except Exception as e:
            failed_requeue.append((tid, str(e)))
    data = {
        "count": len(failed),
        "requeued": len(requeued),
        "failed": len(failed_requeue),
        "requeued_ids": requeued,
        "failed_requeue": failed_requeue,
    }
    def _render(d):
        print(f"Requeued {d['requeued']}/{d['count']} failed tasks")
        if d.get('failed_requeue'):
            print("Failed to requeue:")
            for tid, err in d['failed_requeue']:
                print(f"  {tid[:8]} FAILED: {err}")
    _emit(args, data, _render)


def _ops_restart_orchestrator(args: argparse.Namespace) -> None:
    """Restart background loops."""
    _require_write_config("restart-orchestrator")
    if not _confirm("Restart orchestrator background loops?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", "/api/admin/restart-orchestrator", {})
    _emit(args, result, lambda d: print(json.dumps(d, indent=2, default=str)))


def _ops_circuit_breaker(args: argparse.Namespace) -> None:
    """Show circuit breaker state, optionally reset for a project."""
    if getattr(args, "reset", None):
        _require_write_config("circuit-breaker reset")
        pid = args.reset if args.reset else get_project_id()
        if not _confirm(f"Reset circuit breaker for {pid}?", getattr(args, "yes", False)):
            print("Aborted.")
            return
        result = api("POST", f"/api/admin/projects/{pid}/circuit-breaker/reset", {})
        _emit(args, {"action": "reset", "project_id": pid, "result": result},
              lambda d: print(json.dumps(d.get('result'), indent=2, default=str)))
        return
    data = api("GET", "/api/admin/diagnostics")
    cbs = data.get('circuit_breakers', {})
    cb_list = []
    for pid, cb in cbs.items():
        cb_list.append({
            "project_id": pid,
            "state": cb.get('state', '?'),
            "failure_count": cb.get('failure_count', 0),
            "success_count": cb.get('success_count', 0),
            "opened_at": cb.get('opened_at', ''),
            "cooldown_ends_at": cb.get('cooldown_ends_at', ''),
        })
    def _render(d):
        if not d:
            print("No circuit breakers registered.")
            return
        print("=== CIRCUIT BREAKERS ===")
        for cb in d:
            print(f"  {cb['project_id'][:8]}  state={cb['state']}  "
                  f"failures={cb['failure_count']}  successes={cb['success_count']}")
            if cb['opened_at']:
                print(f"           opened_at={cb['opened_at']}  "
                      f"cooldown_until={cb['cooldown_ends_at'] or '?'}")
    _emit(args, cb_list, _render)


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
        print("Error: a subcommand is required (tail|errors|search|task). "
              "Run `blueprint logs --help` for options.", file=sys.stderr)
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


# ── health ───────────────────────────────────────────────────────────────────

def cmd_health(args: argparse.Namespace) -> None:
    """Dispatch health subcommands: deploy / autonomy / alerts / digest / activity."""
    sub = getattr(args, "health_cmd", None) or "deploy"
    dispatch = {
        "deploy": _health_deploy,
        "autonomy": _health_autonomy,
        "alerts": _health_alerts,
        "digest": _health_digest,
        "activity": _health_activity,
    }
    dispatch[sub](args)


def _age_str(iso_or_dt) -> str:
    """Return human-readable age ('Ns ago' / 'Nm ago') from an ISO string or datetime."""
    if not iso_or_dt:
        return "never"
    try:
        if isinstance(iso_or_dt, str):
            ts = datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
        else:
            ts = iso_or_dt
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return str(iso_or_dt)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m{int(delta % 60)}s ago"
    return f"{int(delta // 3600)}h{int((delta % 3600) // 60)}m ago"


def _health_deploy(args: argparse.Namespace) -> None:
    """Deploy health deep-dive: probe ring buffer, external deps, orchestrator cycle."""
    ds, ds_err = _safe_read("/api/admin/deploy-state")
    ds = ds or {}
    hd, hd_err = _safe_read("/api/health/detailed")
    hd = hd or {}
    orch = (hd.get("components") or {}).get("orchestrator") or {}
    eh = ds.get("external_health") or {}
    data = {
        "status": ds.get("status"),
        "pause_reason": ds.get("pause_reason"),
        "paused_at": ds.get("paused_at"),
        "health_check_interval_seconds": ds.get("health_check_interval_seconds"),
        "health_recovery_streak": ds.get("health_recovery_streak"),
        "health_recovery_streak_required": ds.get("health_recovery_streak_required"),
        "health_recovery_history": ds.get("health_recovery_history") or [],
        "health_recovery_history_size": ds.get("health_recovery_history_size"),
        "health_failure_streak": ds.get("health_failure_streak"),
        "health_failure_streak_threshold": ds.get("health_failure_streak_threshold"),
        "external_health": eh,
        "orchestrator": {
            "status": orch.get("status"),
            "cycle_age_seconds": orch.get("cycle_age_seconds"),
            "queued_tasks": orch.get("queued_tasks"),
            "active_tasks": orch.get("active_tasks"),
        },
        "errors": {k: v for k, v in (("deploy_state", ds_err), ("health_detailed", hd_err)) if v},
    }
    _emit(args, data, _render_health_deploy)


def _render_health_deploy(data: dict) -> None:
    print("=== DEPLOY HEALTH ===")
    print(f"  Status:       {data.get('status', '?')}")
    if data.get("pause_reason"):
        print(f"  Pause reason: {data['pause_reason']}")
    if data.get("paused_at"):
        print(f"  Paused:       {_age_str(data['paused_at'])}")
    eh = data.get("external_health") or {}
    errs = eh.get("errors") or {}
    print("\n=== EXTERNAL DEPS ===")
    for dep in ("github_api", "db", "dispatch_api", "anthropic_api", "openai_api", "gemini_api", "gcp"):
        val = eh.get(dep)
        if val is None:
            continue
        status_str = "OK" if val else "DOWN"
        err_str = errs.get(dep, "")
        suffix = f" — {err_str}" if err_str else ""
        print(f"  {dep:12}: {status_str}{suffix}")
    hist = data.get("health_recovery_history") or []
    size = data.get("health_recovery_history_size") or 5
    streak = data.get("health_recovery_streak") or 0
    required = data.get("health_recovery_streak_required") or 3
    dots = ["OK " if hist[i] else "FAIL" for i in range(min(len(hist), size))]
    dots += ["--  "] * (size - len(dots))
    print(f"\n=== RING BUFFER (3-of-5 gate) ===")
    print(f"  Recovery: [{' | '.join(dots)}]  {streak}/{required} healthy")
    orch = data.get("orchestrator") or {}
    print(f"\n=== ORCHESTRATOR CYCLE ===")
    print(f"  Status:    {orch.get('status', '?')}")
    cycle_age = orch.get("cycle_age_seconds")
    if cycle_age is None:
        print("  Last cycle: never (loop has not completed a cycle since process start)")
    else:
        print(f"  Last cycle: {int(cycle_age)}s ago"
              + ("  WARNING: cycle >30s old — loop may be stuck" if cycle_age > 30 else ""))
    errors = data.get("errors") or {}
    if errors:
        print("\n=== READ ERRORS ===")
        for endpoint, e in errors.items():
            print(f"  {endpoint}: {e}")


def _health_autonomy(args: argparse.Namespace) -> None:
    """Autonomous research state per project."""
    data, err = _safe_read("/api/admin/autonomous-status")
    data = data or {}
    projects = data.get("projects") or []
    global_rate_limit = data.get("global_rate_limit") or {}
    out = {
        "projects": [
            {
                "project_name": p.get("project_name"),
                "autonomous_enabled": p.get("autonomous_enabled"),
                "consecutive_failures": p.get("consecutive_failures", 0),
                "tasks_spawned_last_hour": p.get("tasks_spawned_last_hour", 0),
                "initiative_tasks_spawned_last_hour": p.get(
                    "initiative_tasks_spawned_last_hour", 0
                ),
                "servicing_tasks_spawned_last_hour": p.get(
                    "servicing_tasks_spawned_last_hour", 0
                ),
                "research_create_tasks_spawned_last_hour": p.get(
                    "research_create_tasks_spawned_last_hour", 0
                ),
                "last_research_at": p.get("last_research_at"),
                "global_rate_limit_headroom": global_rate_limit.get("global_headroom"),
                "current_direction": p.get("current_direction"),
            }
            for p in projects
        ],
        "global_rate_limit": global_rate_limit,
        "errors": {"autonomy": err} if err else {},
    }
    _emit(args, out, _render_health_autonomy)


def _render_health_autonomy(data: dict) -> None:
    projects = data.get("projects") or []
    global_rate_limit = data.get("global_rate_limit") or {}
    print("=== AUTONOMY STATE ===")
    if not projects:
        print("  No projects configured.")
        return
    if global_rate_limit:
        print("  Global budget:")
        print(
            f"    total:                {global_rate_limit.get('total', 0)}/"
            f"{global_rate_limit.get('global_cap', '?')} "
            f"(headroom {global_rate_limit.get('global_headroom', '?')})"
        )
        print(
            f"    initiative:           {global_rate_limit.get('initiative', 0)}/"
            f"{global_rate_limit.get('initiative_cap', '?')} "
            f"(headroom {global_rate_limit.get('initiative_headroom', '?')})"
        )
    for p in projects:
        print(f"  Project: {p.get('project_name', '?')}")
        print(f"    enabled:              {p.get('autonomous_enabled')}")
        print(f"    consecutive_failures: {p.get('consecutive_failures', 0)}")
        print(f"    tasks_last_hour:      {p.get('tasks_spawned_last_hour', 0)}")
        print(
            f"    initiative_last_hour: {p.get('initiative_tasks_spawned_last_hour', 0)}"
        )
        print(
            f"    servicing_last_hour:  {p.get('servicing_tasks_spawned_last_hour', 0)}"
        )
        print(
            f"    research_create_hr:   {p.get('research_create_tasks_spawned_last_hour', 0)}"
        )
        print(f"    last_research_at:     {p.get('last_research_at', 'never')}")
        global_headroom = p.get("global_rate_limit_headroom")
        if global_headroom is not None:
            print(f"    global_headroom:      {global_headroom}")
        direction = p.get("current_direction") or {}
        if direction:
            print(f"    direction:            {(direction.get('summary') or '?')[:100]}")


def _health_alerts(args: argparse.Namespace) -> None:
    """Show system alerts; optionally dismiss one or all."""
    dismiss = getattr(args, "dismiss", None)
    dismiss_all = getattr(args, "dismiss_all", False)
    if dismiss_all or dismiss:
        cfg = load_config()
        if cfg.get("read_only"):
            print(
                "ERROR: 'dismiss' is a mutating action but the resolved config is "
                "read-only (agent-JWT fallback). Provide an admin API key "
                "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
                file=sys.stderr,
            )
            sys.exit(1)
        if dismiss_all:
            result = api("POST", "/api/admin/alerts/dismiss-all", {})
            _emit(args, result, lambda d: print(f"Dismissed: {d.get('dismissed_count', '?')} alerts"))
            return
        result = api("POST", f"/api/admin/alerts/{dismiss}/dismiss", {})
        _emit(args, result, lambda d: print(f"Dismissed: {d.get('status', 'ok')}"))
        return
    alerts, err = _safe_read("/api/admin/alerts")
    alerts = alerts if isinstance(alerts, list) else []
    out = {"alerts": alerts, "errors": {"alerts": err} if err else {}}
    _emit(args, out, _render_health_alerts)


def _render_health_alerts(data: dict) -> None:
    alerts = data.get("alerts") or []
    if not alerts:
        print("No active alerts.")
        return
    print(f"=== ALERTS ({len(alerts)}) ===")
    for a in alerts:
        severity = a.get("severity", "?")
        title = a.get("title", "?")
        msg = (a.get("message") or "")[:120]
        ts = (a.get("created_at") or "")[:19]
        aid = (a.get("id") or "?")[:8]
        tid = (a.get("task_id") or "")[:8]
        task_ref = f"  task={tid}" if tid else ""
        print(f"  [{severity:8}] {aid}  {ts}  {title}{task_ref}")
        if msg:
            print(f"             {msg}")


def _health_digest(args: argparse.Namespace) -> None:
    """Catch-up digest for the last N hours."""
    hours = getattr(args, "hours", 24) or 24
    pid = get_project_id(getattr(args, "project_id", None))
    data, err = _safe_read(f"/api/admin/digest?project_id={pid}&hours={hours}")
    data = data or {}
    out = {
        "hours": hours,
        "completed_count": data.get("completed_count"),
        "failed_count": data.get("failed_count"),
        "prs_opened": data.get("prs_opened"),
        "research_cycles": data.get("research_cycles"),
        "pending_approval_count": data.get("pending_approval_count"),
        "active_count": data.get("active_count"),
        "running_count": data.get("running_count"),
        "queued_count": data.get("queued_count"),
        "needs_attention": data.get("needs_attention") or [],
        "recent_completions": data.get("recent_completions") or [],
        "recent_failures": data.get("recent_failures") or [],
        "errors": {"digest": err} if err else {},
    }
    _emit(args, out, _render_health_digest)


def _render_health_digest(data: dict) -> None:
    hours = data.get("hours", 24)
    print(f"=== DIGEST (last {hours}h) ===")
    print(f"  Completed:        {data.get('completed_count', '?')}")
    print(f"  Failed:           {data.get('failed_count', '?')}")
    print(f"  PRs opened:       {data.get('prs_opened', '?')}")
    print(f"  Research cycles:  {data.get('research_cycles', '?')}")
    print(f"  Pending approval: {data.get('pending_approval_count', '?')}")
    print(f"  Active now:       {data.get('active_count', '?')} "
          f"({data.get('running_count', '?')} running, {data.get('queued_count', '?')} queued)")
    for item in (data.get("needs_attention") or []):
        reason = item.get("reason", "?")
        title = (item.get("title") or "?")[:55]
        wait_h = item.get("waiting_hours")
        tid = (item.get("task_id") or "?")[:8]
        wait_str = f"{wait_h}h ago" if wait_h is not None else "?"
        print(f"\n--- Needs Attention ---") if item == (data.get("needs_attention") or [])[0] else None
        print(f"  {tid}  [{reason:18}]  {title}  ({wait_str})")
    for c in (data.get("recent_completions") or []):
        print(f"\n--- Recent Completions ---") if c == (data.get("recent_completions") or [])[0] else None
        tid = (c.get("task_id") or "?")[:8]
        title = (c.get("title") or "?")[:50]
        ttype = c.get("task_type", "?")
        pr = c.get("pr_url", "")
        ts = (c.get("completed_at") or "")[:16]
        pr_str = f"  {pr}" if pr else ""
        print(f"  {tid}  [{ttype:8}]  {ts}  {title}{pr_str}")
    for f in (data.get("recent_failures") or []):
        print(f"\n--- Recent Failures ---") if f == (data.get("recent_failures") or [])[0] else None
        tid = (f.get("task_id") or "?")[:8]
        title = (f.get("title") or "?")[:50]
        ts = (f.get("failed_at") or "")[:16]
        err = (f.get("error_snippet") or "")[:100]
        print(f"  {tid}  {ts}  {title}")
        if err:
            print(f"           {err}")


def _health_activity(args: argparse.Namespace) -> None:
    """Recent activity feed: completions, failures, research events."""
    limit = getattr(args, "limit", 20) or 20
    data, err = _safe_read(f"/api/admin/activity?limit={limit}")
    events = data.get("events", []) if isinstance(data, dict) else (data or [])
    out = {
        "events": events,
        "total": data.get("total") if isinstance(data, dict) else None,
        "errors": {"activity": err} if err else {},
    }
    _emit(args, out, _render_health_activity)


_ACTIVITY_ICONS = {
    "TASK_COMPLETED": "done", "TASK_FAILED": "FAIL",
    "RESEARCH_FINDINGS": "research", "DEPLOY": "deploy",
}


def _render_health_activity(data: dict) -> None:
    events = data.get("events") or []
    if not events:
        print("No recent activity.")
        return
    print(f"=== ACTIVITY (last {len(events)} events) ===")
    for e in events:
        etype = e.get("type", "?")
        icon = _ACTIVITY_ICONS.get(etype, etype)
        ts = (e.get("timestamp") or "")[:16]
        title = (e.get("title") or "?")[:55]
        tid = (e.get("task_id") or "")[:8]
        pr = e.get("pr_url") or ""
        err = (e.get("error_summary") or "")[:80]
        dur = e.get("duration_seconds")
        dur_str = f"  {int(dur)}s" if dur else ""
        pr_str = f"  {pr}" if pr else ""
        print(f"  {ts}  [{icon:10}]  {tid}  {title}{dur_str}{pr_str}")
        if err:
            print(f"                              {err}")


# ── analytics ────────────────────────────────────────────────────────────────

def cmd_analytics(args: argparse.Namespace) -> None:
    """Dispatch analytics subcommands: cost / duration / pause-stats / failing-types."""
    sub = getattr(args, "analytics_cmd", None) or "cost"
    pid = get_project_id(getattr(args, "project_id", None))

    if sub == "cost":
        window = getattr(args, "window", "1h")
        data, err = _safe_read(f"/api/admin/task-cost-breakdown?project_id={pid}&window={window}")
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return
        _emit(args, data, _render_analytics_cost)

    elif sub == "duration":
        data, err = _safe_read(f"/api/admin/task-duration-stats?project_id={pid}")
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return
        _emit(args, data, _render_analytics_duration)

    elif sub == "pause-stats":
        hours = getattr(args, "hours", 24)
        data, err = _safe_read(
            f"/api/admin/direction-auto-pause-stats?project_id={pid}&window_hours={hours}"
        )
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return
        _emit(args, data, _render_analytics_pause_stats)

    elif sub == "failing-types":
        data, err = _safe_read("/api/admin/failing-task-types")
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return
        _emit(args, data, _render_analytics_failing_types)

    else:
        print(
            f"analytics: unknown subcommand '{sub}'. Run `blueprint analytics --help`.",
            file=sys.stderr,
        )


def _render_analytics_cost(data: dict) -> None:
    window = data.get("window", "?")
    total = data.get("total_estimated_cost_usd", 0.0)
    task_count = data.get("task_count", 0)
    print(f"=== TASK COST BREAKDOWN (window={window}, {task_count} tasks) ===")
    print(f"  Total estimated cost: ${total:.4f}")
    for row in data.get("task_type_breakdown") or []:
        tt = row.get("task_type", "?")
        cost = row.get("estimated_cost_usd", 0.0)
        n = row.get("task_count", 0)
        print(f"  {tt:20}  ${cost:.4f}  ({n} tasks)")
    top = data.get("top_tasks") or []
    if top:
        print("  --- top tasks ---")
        for t in top:
            print(f"  {t.get('task_id','')[:8]}  ${t.get('estimated_cost_usd',0):.4f}  {(t.get('task_title') or '')[:60]}")


def _render_analytics_duration(data: dict) -> None:
    print("=== TASK DURATION STATS (last 7 days) ===")
    for tt, s in (data.get("stats") or {}).items():
        p50 = s.get("p50_seconds", 0)
        n = s.get("sample_count", 0)
        print(f"  {tt:20}  p50={int(p50)}s  n={n}")
    scope = data.get("scope_duration_stats") or {}
    if scope:
        print("  --- by scope ---")
        for sc, s in scope.items():
            p50 = s.get("p50_seconds", 0)
            p90 = s.get("p90_seconds", 0)
            n = s.get("sample_size", 0)
            print(f"  {sc:10}  p50={int(p50)}s  p90={int(p90)}s  n={n}")


def _render_analytics_pause_stats(data: dict) -> None:
    window = data.get("window_hours", "?")
    paused = data.get("auto_paused_count", 0)
    recovered = data.get("auto_recovered_count", 0)
    retired = data.get("auto_retired_count", 0)
    current = data.get("currently_auto_paused", 0)
    trend = data.get("trend", "?")
    print(f"=== DIRECTION AUTO-PAUSE STATS (window={window}h) ===")
    print(f"  auto-paused={paused}  recovered={recovered}  retired={retired}")
    print(f"  currently paused={current}  trend={trend}")
    for r in data.get("top_reasons") or []:
        print(f"  reason: {r.get('reason','?')}  ({r.get('count',0)}x)")


def _render_analytics_failing_types(data: dict) -> None:
    gated = data.get("gated_types") or []
    threshold = data.get("global_failure_threshold", "?")
    tightened = data.get("anomaly_tightened", False)
    window = data.get("window_minutes", "?")
    print(f"=== FAILING TASK TYPES (last {window}min, global_threshold={threshold}"
          f"{', anomaly-tightened' if tightened else ''}) ===")
    if not gated:
        print("  (no types currently gated)")
        return
    for g in gated:
        tt = g.get("task_type", "?")
        rate = g.get("failure_rate", 0)
        n = g.get("sample_size", 0)
        print(f"  {tt:20}  failure_rate={rate:.1%}  n={n}")


# ── vision ────────────────────────────────────────────────────────────────────

def cmd_vision(args: argparse.Namespace) -> None:
    """Show product vision and design document."""
    pid = get_project_id(getattr(args, "project_id", None))
    data = api("GET", f"/api/admin/projects/{pid}/directions")

    design_doc_key = getattr(args, "design_doc_key", None)
    if design_doc_key is not None:
        doc = data.get("design_doc") or {}
        if not doc:
            print("No design document yet.")
            return
        if design_doc_key:
            page = doc.get(design_doc_key)
            if page is None:
                print(f"Page not found: {design_doc_key}")
                print(f"Available pages: {', '.join(sorted(doc.keys()))}")
                return
            print(f"=== DESIGN DOC: {design_doc_key} ===")
            _emit(args, page, lambda p: print(json.dumps(p, indent=2)))
        else:
            _emit(args, doc, _render_design_doc)
        return

    vision = data.get("vision") or {}
    if not vision:
        print("No vision set yet.")
        return
    _emit(args, vision, _render_vision)


def _render_design_doc(doc: dict) -> None:
    print(f"=== DESIGN DOCUMENT ({len(doc)} pages) ===")
    for key in sorted(doc.keys()):
        page = doc[key]
        if isinstance(page, dict):
            summary = page.get("summary", page.get("description", str(page)[:120]))
            status = page.get("status", "")
            status_str = f" [{status}]" if status else ""
            print(f"  {key}{status_str} — {str(summary)[:100]}")
        else:
            print(f"  {key} — {str(page)[:100]}")


def _render_vision(vision: dict) -> None:
    print("=== PRODUCT VISION ===")
    print(f"  Purpose:  {vision.get('purpose', '(not set)')}")
    focus = vision.get("focus")
    if focus:
        if isinstance(focus, list):
            print(f"  Focus ({len(focus)} active):")
            for f in focus:
                print(f"    - {f}")
        else:
            print(f"  Focus:    {focus}")
    print(f"  Revision: {vision.get('revision', '?')}")
    print(f"  Updated:  {(vision.get('last_updated') or '?')[:19]}")
    criteria = vision.get("success_criteria", [])
    if criteria:
        print(f"\n  Success criteria ({len(criteria)}):")
        for c in criteria:
            print(f"    - {c}")
    context = vision.get("context", "")
    if context:
        print(f"\n  Context:\n    {context[:500]}")
    cycles = vision.get("_strategic_cycle_count", 0)
    spawns = vision.get("_research_spawn_count", 0)
    if cycles or spawns:
        print(f"\n  Stats: {spawns} research spawns, {cycles} strategic cycles")


# ── strategic-cycle ───────────────────────────────────────────────────────────

def cmd_strategic_cycle(args: argparse.Namespace) -> None:
    """Manually trigger a strategic cycle."""
    _require_write_config("strategic-cycle")
    phase = getattr(args, "phase", "design_doc") or "design_doc"
    pid = get_project_id(getattr(args, "project_id", None))
    if not _confirm(f"Trigger strategic-cycle (phase={phase})?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", f"/api/admin/projects/{pid}/strategic-cycle?phase={phase}")
    out = {"phase": phase, **result}
    _emit(args, out, _render_strategic_cycle)


def _render_strategic_cycle(d: dict) -> None:
    phase = d.get("phase", "?")
    task_id = d.get("task_id") or d.get("id") or "?"
    print(f"Spawned {phase} task: {task_id}")
    print(f"  Title: {d.get('title', '?')}")
    if phase == "design_doc":
        print("  Phase 1 will chain into strategic_review (Phase 2) on completion.")


# ── strategic (resident session: start / stop / chat) ─────────────────────────

def cmd_strategic(args: argparse.Namespace) -> None:
    """Dispatch resident Strategic session subcommands: start / stop / chat."""
    sub = getattr(args, "strategic_cmd", None)
    if sub == "start":
        _strategic_start(args)
    elif sub == "stop":
        _strategic_stop(args)
    elif sub == "chat":
        _strategic_chat(args)
    else:
        print("Usage: blueprint strategic {start|stop|chat}", file=sys.stderr)
        sys.exit(1)


def _strategic_start(args: argparse.Namespace) -> None:
    """Start (or adopt) a resident Strategic session on the operator's runner."""
    _require_write_config("strategic start")
    pid = get_project_id(getattr(args, "project_id", None))
    body: dict = {}
    runner = getattr(args, "runner_id", None)
    if runner:
        body["runner_id"] = runner
    result = api("POST",
                 f"/api/admin/projects/{pid}/strategic-session/start",
                 body or None)
    _emit(args, result, _render_strategic_session_start)


def _render_strategic_session_start(d: dict) -> None:
    print("=== STRATEGIC SESSION STARTED ===")
    enabled = d.get("enabled")
    if enabled is not None:
        print(f"  enabled:    {enabled}")
    sess = d.get("session") or {}
    if not sess:
        print("  (no active session returned)")
        return
    print(f"  session_id: {sess.get('session_id', '?')}")
    print(f"  task_id:    {sess.get('task_id', '?')}")
    print(f"  runner:     {sess.get('runner_id') or '-'}")
    print(f"  status:     {sess.get('status', '?')}")
    if sess.get("started_at"):
        print(f"  started_at: {sess['started_at']}")


def _strategic_stop(args: argparse.Namespace) -> None:
    """Stop the resident Strategic session and release authority."""
    _require_write_config("strategic stop")
    pid = get_project_id(getattr(args, "project_id", None))
    if not _confirm("Stop the resident Strategic session?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", f"/api/admin/projects/{pid}/strategic-session/stop")
    _emit(args, result, _render_strategic_session_stop)


def _render_strategic_session_stop(d: dict) -> None:
    print("=== STRATEGIC SESSION STOPPED ===")
    sess = d.get("session") or {}
    if not sess:
        print("  (no active session)")
        return
    print(f"  session_id: {sess.get('session_id', '?')}")
    print(f"  status:     {sess.get('status', '?')}")
    if sess.get("ended_at"):
        print(f"  ended_at:   {sess['ended_at']}")


_CHAT_ROLE_LABELS = {
    "user": "you",
    "strategic": "strategist",
    "agent": "agent",
    "system": "system",
}


def _render_chat_message(m: dict, json_mode: bool = False) -> None:
    if json_mode:
        print(json.dumps(m, default=str))
        return
    role = _CHAT_ROLE_LABELS.get(m.get("from_type"), m.get("from_type") or "?")
    content = (m.get("content") or "").strip()
    ts = (m.get("created_at") or "")[:19]
    ts_str = f"  [{ts}]" if ts else ""
    print(f"\n{role}>{ts_str}\n{content}")


def _strategic_chat(args: argparse.Namespace) -> None:
    """Interactive terminal chat with the resident strategist.

    Sends each typed line to the strategic-session messages endpoint, then polls
    the session conversation (``GET .../strategic-session`` → ``conversation.items``)
    and prints new strategist / agent / system replies as they arrive. An empty
    line keeps polling without sending; ``/quit`` (or EOF / Ctrl-C) exits.
    """
    _require_write_config("strategic chat")
    pid = get_project_id(getattr(args, "project_id", None))
    json_mode = _json_mode(args)
    interval = max(0.5, float(getattr(args, "interval", 3.0) or 3.0))
    reply_timeout = max(interval, float(getattr(args, "timeout", 120.0) or 120.0))
    msg_type = getattr(args, "type", None) or "NOTE"

    seen: set = set()

    def _conversation_items() -> list:
        resp = api("GET", f"/api/admin/projects/{pid}/strategic-session")
        conv = resp.get("conversation") or {}
        return conv.get("items") or []

    def _print_new(items: list) -> int:
        """Print unseen non-operator messages oldest-first; return how many printed."""
        fresh = [m for m in items if m.get("id") and m["id"] not in seen]
        fresh.sort(key=lambda x: (x.get("created_at") or "", str(x.get("id"))))
        printed = 0
        for m in fresh:
            seen.add(m["id"])
            if m.get("from_type") == "user":
                continue  # skip echoing the operator's own messages
            _render_chat_message(m, json_mode)
            printed += 1
        return printed

    # Seed the seen-set with existing history so we only surface NEW replies.
    try:
        for m in _conversation_items():
            if m.get("id"):
                seen.add(m["id"])
    except APIError:
        return

    print("blueprint chat — resident strategist. Type a message and press Enter; "
          "empty line polls, /quit or Ctrl-C exits.", file=sys.stderr)
    try:
        while True:
            try:
                line = input("you> ")
            except EOFError:
                print(file=sys.stderr)
                break
            content = line.strip()
            if content in ("/quit", "/exit", "/q"):
                break
            if content:
                sent = api("POST",
                           f"/api/admin/projects/{pid}/strategic-session/messages",
                           {
                               "content": content,
                               "message_type": msg_type,
                               "idempotency_key": f"strategic-chat:{uuid.uuid4()}",
                           })
                sent_id = sent.get("id") if isinstance(sent, dict) else None
                if sent_id:
                    seen.add(sent_id)
            # Poll for the strategist's reply until it goes quiet or times out.
            waited = 0.0
            replied = False
            while waited < reply_timeout:
                time.sleep(interval)
                waited += interval
                try:
                    printed = _print_new(_conversation_items())
                except APIError:
                    break
                if printed:
                    replied = True
                elif replied:
                    # A quiet poll after a reply → treat the turn as complete.
                    break
            if not replied:
                print("  (no reply yet — the strategist may still be working; "
                      "press Enter to keep waiting or type another message)",
                      file=sys.stderr)
    except KeyboardInterrupt:
        print("\nExiting chat.", file=sys.stderr)


# ── trim-playbook-archive ─────────────────────────────────────────────────────

def cmd_trim_playbook_archive(args: argparse.Namespace) -> None:
    """Trim design_doc.playbook_archive to the last N entries."""
    _require_write_config("trim-playbook-archive")
    keep = getattr(args, "keep", 50) or 50
    dry_run = getattr(args, "dry_run", False)
    pid = get_project_id(getattr(args, "project_id", None))

    data = api("GET", f"/api/admin/projects/{pid}/directions")
    doc = data.get("design_doc") or {}
    archive = doc.get("playbook_archive") or []
    before_bytes = len(json.dumps(archive).encode())

    print(f"Current playbook_archive: {len(archive)} entries ({before_bytes:,} bytes)")
    if len(archive) <= keep:
        print(f"Already within cap of {keep} — nothing to trim.")
        return

    trimmed = archive[-keep:]
    after_bytes = len(json.dumps(trimmed).encode())
    dropped = len(archive) - len(trimmed)
    print(f"Would drop {dropped} oldest -> {len(trimmed)} remain ({after_bytes:,} bytes)")

    if dry_run:
        print("(dry-run — no changes written)")
        return

    new_doc = {**doc, "playbook_archive": trimmed}
    api("PATCH", f"/api/admin/projects/{pid}", {"design_doc": new_doc})
    print(f"Trimmed archive to {len(trimmed)} entries ({before_bytes - after_bytes:,} bytes freed)")


# ── fingerprints ──────────────────────────────────────────────────────────────

def _resolve_fingerprint(query: str, project_id: str | None = None) -> dict | None:
    """Find a residual case by fingerprint substring (case-insensitive)."""
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


def cmd_fingerprints(args: argparse.Namespace) -> None:
    """List or manage residual remediation cases."""
    from urllib.parse import quote
    pid = get_project_id(getattr(args, "project_id", None))
    ignore = getattr(args, "ignore", None)
    unignore = getattr(args, "unignore", None)
    clear = getattr(args, "clear", False)

    if ignore:
        _require_write_config("fingerprints --ignore")
        case = _resolve_fingerprint(ignore, pid)
        if not case:
            return
        result = api("POST", f"/api/admin/residual-cases/{quote(case['fingerprint'], safe='')}/ignore?project_id={pid}")
        _emit(args, result, lambda r: print(
            f"Ignored: {r.get('fingerprint', '')[:100]} → status={r.get('status')}"
        ))
        return

    if unignore:
        _require_write_config("fingerprints --unignore")
        ignored_path = f"/api/admin/residual-cases?status=IGNORED&limit=200&project_id={pid}"
        cases = api("GET", ignored_path).get("cases", [])
        needle = unignore.lower()
        matches = [c for c in cases if needle in (c.get("fingerprint") or "").lower()]
        if not matches:
            print(f"No IGNORED case matches: {unignore!r}")
            return
        if len(matches) > 1:
            print(f"Ambiguous — {len(matches)} IGNORED cases match. Use a longer substring.")
            return
        result = api("POST", f"/api/admin/residual-cases/{quote(matches[0]['fingerprint'], safe='')}/unignore?project_id={pid}")
        _emit(args, result, lambda r: print(
            f"Unignored: {r.get('fingerprint', '')[:100]} → status={r.get('status')}"
        ))
        return

    if clear:
        _require_write_config("fingerprints --clear")
        result = api("POST", "/api/admin/residual-fingerprints/clear")
        _emit(args, result, lambda r: print(f"Cleared in-memory store: {r}"))
        return

    show_all = getattr(args, "all_statuses", False)
    statuses = getattr(args, "status", None) or []
    path = "/api/admin/residual-cases?limit=100"
    _ALL_FP_STATUSES = ("ACTIVE", "REMEDIATING", "SUSPENDED", "IGNORED",
                        "RESOLVED", "CLOSED_ORGANICALLY", "MERGED", "FAILED")
    if show_all:
        for s in _ALL_FP_STATUSES:
            path += f"&status={s}"
    else:
        for s in statuses:
            path += f"&status={s}"
    result = api("GET", path)
    cases = result.get("cases", [])
    filt = result.get("status_filter", [])
    _emit(args, {"cases": cases, "status_filter": filt}, _render_fingerprints)


def _render_fingerprints(data: dict) -> None:
    cases = data.get("cases") or []
    filt = data.get("status_filter") or []
    print(f"=== RESIDUAL CASES ({len(cases)}) status={','.join(filt)} ===")
    if not cases:
        print("(none)")
        return
    for c in cases:
        fp = (c.get("fingerprint") or "").replace("\n", " ")
        print(f"  [{c['status']:18}] hits={c['hit_count']:<3} "
              f"consec={c.get('consecutive_failure_count', 0)}  "
              f"last_seen={(c.get('last_seen_at') or '')[:19]}  "
              f"pr={c.get('last_spawned_pr_number') or '-'}")
        print(f"    fp: {fp[:200]}")


# ── regression-cases ──────────────────────────────────────────────────────────

def cmd_regression_cases(args: argparse.Namespace) -> None:
    """List or close regression remediation cases."""
    from urllib.parse import quote
    pid = get_project_id(getattr(args, "project_id", None))

    def _build_close_path(cls: str, reason: str | None) -> str:
        p = (f"/api/admin/regression-cases/close?project_id={pid}"
             f"&regression_class={quote(cls, safe='')}")
        if reason:
            p += f"&reason={quote(reason, safe='')}"
        return p

    close = getattr(args, "close", None)
    close_prefix = getattr(args, "close_prefix", None)
    reason = getattr(args, "reason", None)

    if close:
        _require_write_config("regression-cases --close")
        try:
            result = api("POST", _build_close_path(close, reason))
            _emit(args, result, lambda r: print(
                f"Closed: {r.get('regression_class', '')[:120]} → "
                f"{r.get('prior_status')} → {r.get('status')}"
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"Error closing {close!r}: {exc}", file=sys.stderr)
        return

    if close_prefix:
        _require_write_config("regression-cases --close-prefix")
        path = f"/api/admin/regression-cases?project_id={pid}&limit=200"
        for s in ("ACTIVE", "REMEDIATING", "NEEDS_MANUAL_INTERVENTION"):
            path += f"&status={s}"
        result = api("GET", path)
        cases = result.get("cases", [])
        matches = [c for c in cases
                   if c.get("regression_class", "").startswith(close_prefix)]
        if not matches:
            print(f"No open cases match prefix {close_prefix!r}")
            return
        print(f"Closing {len(matches)} case(s) matching prefix {close_prefix!r}:")
        for c in matches:
            cls = c["regression_class"]
            try:
                r = api("POST", _build_close_path(cls, reason))
                print(f"  {r.get('prior_status'):28} → {r.get('status'):8}  {cls[:80]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR  {cls[:80]}: {exc}")
        return

    show_all = getattr(args, "all_statuses", False)
    statuses = getattr(args, "status", None) or []
    path = f"/api/admin/regression-cases?project_id={pid}&limit=200"
    if show_all:
        for s in ("ACTIVE", "REMEDIATING", "NEEDS_MANUAL_INTERVENTION", "CLOSED", "MERGED"):
            path += f"&status={s}"
    else:
        for s in statuses:
            path += f"&status={s}"
    result = api("GET", path)
    cases = result.get("cases", [])
    filt = result.get("status_filter", [])
    _emit(args, {"cases": cases, "status_filter": filt}, _render_regression_cases)


def _render_regression_cases(data: dict) -> None:
    cases = data.get("cases") or []
    filt = data.get("status_filter") or []
    print(f"=== REGRESSION CASES ({len(cases)}) status={','.join(filt)} ===")
    if not cases:
        print("(none)")
        return
    for c in cases:
        cls = (c.get("regression_class") or "").replace("\n", " ")
        print(f"  [{c['status']:28}] attempts={c.get('attempts', 0)}  "
              f"hits={c.get('hit_count', 0)}  "
              f"last_seen={(c.get('last_seen_at') or '')[:19]}  "
              f"pr={c.get('last_remediation_pr_number') or '-'}")
        print(f"    class: {cls[:200]}")


# ── playbook ──────────────────────────────────────────────────────────────────

def cmd_playbook(args: argparse.Namespace) -> None:
    """List, show, or delete playbook entries."""
    pid = get_project_id(getattr(args, "project_id", None))
    url = f"/api/admin/projects/{pid}/playbook"
    sub = getattr(args, "playbook_cmd", None)

    if sub == "delete":
        _require_write_config("playbook delete")
        key = args.key
        api("DELETE", f"{url}/{key}")
        _emit(args, {"key": key}, lambda r: print(f"Deleted: {key}"))
        return

    if sub == "prune":
        import re
        _require_write_config("playbook prune")
        pattern_str = args.pattern
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
        except re.error as exc:
            print(f"Invalid pattern {pattern_str!r}: {exc}", file=sys.stderr)
            sys.exit(1)
        data = api("GET", url)
        entries = data.get("entries", {})
        matched = []
        for k, v in entries.items():
            content = v if isinstance(v, str) else (v or {}).get("content", "")
            if pattern.search(k) or pattern.search(content):
                matched.append((k, content))
        if not matched:
            print(f"No entries matching '{pattern_str}'.")
            return
        if not _confirm(
            f"Delete {len(matched)} entries matching '{pattern_str}'?",
            getattr(args, "yes", False),
        ):
            print("Aborted.")
            return
        for k, _ in matched:
            api("DELETE", f"{url}/{k}")
            print(f"  Deleted: {k}")
        print("Done.")
        return

    data = api("GET", url)
    entries = data.get("entries", {})
    _emit(args, {"entries": entries}, _render_playbook)


def _render_playbook(data: dict) -> None:
    entries = data.get("entries") or {}
    if not entries:
        print("No playbook entries.")
        return
    print(f"Playbook entries ({len(entries)}):")
    for k, v in sorted(entries.items()):
        content = v if isinstance(v, str) else (v or {}).get("content", "")
        print(f"  {k}: {(content or '')[:120]}")


# ── knowledge ────────────────────────────────────────────────────────────────

def cmd_knowledge(args: argparse.Namespace) -> None:
    """Dispatch knowledge subcommands: list / show / sweep."""
    sub = getattr(args, "knowledge_cmd", None) or "list"
    if sub == "show":
        _knowledge_show(args)
    elif sub == "sweep":
        _knowledge_sweep(args)
    else:
        _knowledge_list(args)


def _knowledge_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    path = f"/api/admin/projects/{pid}/knowledge-documents?limit={getattr(args, 'limit', 20)}"
    if getattr(args, "direction", None):
        path += f"&direction={args.direction}"
    if getattr(args, "source_kind", None):
        path += f"&source_kind={args.source_kind}"
    if getattr(args, "q", None):
        from urllib.parse import quote
        path += f"&q={quote(args.q)}"
    if getattr(args, "topic", None):
        from urllib.parse import quote
        path += f"&topic={quote(args.topic)}"
    if getattr(args, "tag", None):
        from urllib.parse import quote
        path += f"&tag={quote(args.tag)}"
    resp = api("GET", path)
    docs = resp.get("documents", resp) if isinstance(resp, dict) else resp
    total = resp.get("total") if isinstance(resp, dict) else None
    _emit(args, {"documents": docs, "total": total}, _render_knowledge_list)


def _render_knowledge_list(data: dict) -> None:
    docs = data.get("documents") or []
    total = data.get("total")
    suffix = f" / {total} total" if total is not None else ""
    print(f"=== KNOWLEDGE LIBRARY ({len(docs)} shown{suffix}) ===")
    if not docs:
        print("  (none)")
        return
    for d in docs:
        did = (d.get("id") or "?")
        kind = d.get("source_kind", "?")
        title = (d.get("title") or "")[:60]
        direction = (d.get("direction_id") or "")[:12]
        created = (d.get("created_at") or "")[:10]
        topic = d.get("topic") or ""
        tags = d.get("tags") or []
        dir_str = f"  dir={direction}" if direction else ""
        topic_str = f"  topic={topic}" if topic else ""
        tag_str = "  " + " ".join(f"#{t}" for t in tags[:2]) if tags else ""
        print(f"  {did}  [{kind:12}] {created}  {title}{dir_str}{topic_str}{tag_str}")


def _knowledge_show(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    doc_id = args.doc_id
    if len(doc_id) < 36:
        resp = api("GET", f"/api/admin/projects/{pid}/knowledge-documents?limit=200")
        docs = resp.get("documents", resp) if isinstance(resp, dict) else resp
        match = next((d for d in (docs or []) if (d.get("id") or "").startswith(doc_id)), None)
        if match is None:
            print(f"No knowledge document found matching '{doc_id}'.", file=sys.stderr)
            return
        doc_id = match["id"]
    doc = api("GET", f"/api/admin/projects/{pid}/knowledge-documents/{doc_id}")
    _emit(args, doc, _render_knowledge_doc)


def _render_knowledge_doc(doc: dict) -> None:
    print(f"=== KNOWLEDGE: {doc.get('id', '?')} ===")
    print(f"  Title:       {doc.get('title', '?')}")
    print(f"  Source kind: {doc.get('source_kind', '?')}")
    created = (doc.get("created_at") or "")[:19]
    if created:
        print(f"  Created:     {created}")
    direction = doc.get("direction_id")
    if direction:
        print(f"  Direction:   {direction}")
    topic = doc.get("topic")
    if topic:
        print(f"  Topic:       {topic}")
    tags = doc.get("tags") or []
    if tags:
        print(f"  Tags:        {' '.join(f'#{t}' for t in tags)}")
    summary = doc.get("summary")
    if summary:
        print("\n  Summary:")
        for line in summary.splitlines():
            print(f"    {line}")
    body = doc.get("body") or doc.get("content") or ""
    if body:
        print(f"\n{body}")
    citations = doc.get("source_citations") or []
    if citations:
        print("\n  Citations:")
        for i, c in enumerate(citations, 1):
            if isinstance(c, dict):
                ref = c.get("ref") or c.get("url") or c.get("title") or str(c)
            else:
                ref = str(c)
            print(f"    {i}. {ref}")


def _knowledge_sweep(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    sweep_id = args.sweep_id
    path = (
        f"/api/admin/projects/{pid}/knowledge-documents"
        f"?sweep_id={sweep_id}&source_kind=compute_experiment&limit=200"
    )
    resp = api("GET", path)
    docs = resp.get("documents", resp) if isinstance(resp, dict) else resp
    _emit(args, {"sweep_id": sweep_id, "documents": docs or []}, _render_knowledge_sweep)


def _render_knowledge_sweep(data: dict) -> None:
    sweep_id = data.get("sweep_id", "?")
    docs = data.get("documents") or []
    print(f"=== SWEEP ARMS: {sweep_id} ===")
    if not docs:
        print("  (no arm results indexed for this sweep)")
        return

    # Build arm rows from per-arm docs (arm_label set) and/or the aggregate
    # doc whose forward_quality_verdict['arms'] block carries per-arm results.
    rows: list[dict] = []
    for doc in docs:
        arm_label = doc.get("arm_label")
        verdict = doc.get("forward_quality_verdict") or {}
        if arm_label:
            # Per-arm document
            rows.append({
                "arm": arm_label,
                "metric": _sweep_metric_value(verdict),
                "criterion_met": verdict.get("criterion_met"),
                "cost_usd": verdict.get("cost_usd"),
            })
        else:
            # Aggregate document — per_arm_verdicts is authoritative for criterion/cost
            per_arm_verdicts = verdict.get("per_arm_verdicts") or {}
            arms_block = verdict.get("arms") or {}
            if isinstance(arms_block, dict):
                for name, arm_data in arms_block.items():
                    if isinstance(arm_data, dict):
                        pav = per_arm_verdicts.get(name) if isinstance(per_arm_verdicts, dict) else {}
                        rows.append({
                            "arm": name,
                            "metric": _sweep_metric_value(arm_data),
                            "criterion_met": pav.get("met") if pav else arm_data.get("criterion_met"),
                            "cost_usd": pav.get("cost_usd") if pav else arm_data.get("cost_usd"),
                        })
            elif isinstance(arms_block, list):
                for arm_data in arms_block:
                    if isinstance(arm_data, dict):
                        arm_name = arm_data.get("arm") or arm_data.get("arm_label") or arm_data.get("name") or "?"
                        pav = per_arm_verdicts.get(arm_name) if isinstance(per_arm_verdicts, dict) else {}
                        rows.append({
                            "arm": arm_name,
                            "metric": _sweep_metric_value(arm_data),
                            "criterion_met": pav.get("met") if pav else arm_data.get("criterion_met"),
                            "cost_usd": pav.get("cost_usd") if pav else arm_data.get("cost_usd"),
                        })

    if not rows:
        print("  (no per-arm results found in indexed documents)")
        return

    col_arm = max(len(r["arm"]) for r in rows)
    col_arm = max(col_arm, 4)
    header = f"  {'ARM':<{col_arm}}  {'METRIC':>14}  {'CRITERION':>9}  {'COST (USD)':>10}"
    print(header)
    print("  " + "-" * (col_arm + 14 + 9 + 10 + 9))
    for r in rows:
        arm = r["arm"]
        metric = r["metric"]
        met = r["criterion_met"]
        if met is True:
            crit_str = "MET"
        elif met is False:
            crit_str = "NOT MET"
        else:
            crit_str = "?"
        cost = r["cost_usd"]
        cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "?"
        metric_str = f"{metric:.4f}" if isinstance(metric, (int, float)) else (str(metric) if metric is not None else "?")
        print(f"  {arm:<{col_arm}}  {metric_str:>14}  {crit_str:>9}  {cost_str:>10}")


def _sweep_metric_value(verdict: dict) -> "float | str | None":
    """Extract the primary metric value from a per-arm verdict or arm-block entry."""
    # Direct scalar value
    if "value" in verdict:
        return verdict["value"]
    # result_metrics: prefer the first non-'arms' scalar
    metrics = verdict.get("result_metrics") or {}
    if isinstance(metrics, dict):
        for k, v in metrics.items():
            if k != "arms" and isinstance(v, (int, float)):
                return v
    # criterion carries threshold — not a measured value, skip
    criterion = verdict.get("criterion") or {}
    if isinstance(criterion, dict) and "value" in criterion:
        return criterion["value"]
    return None


# ── compute ───────────────────────────────────────────────────────────────────

_ACTIVE_COMPUTE_STATUSES = frozenset({"QUEUED", "STARTING", "RUNNING", "POLLING"})


def _compute_job_burn_rate(j: dict, now: "datetime") -> "float | None":
    """Return $/hr burn rate for an active job; None if data is insufficient (<60s elapsed or no accrued cost)."""
    if j.get("status") not in _ACTIVE_COMPUTE_STATUSES:
        return None
    started_at_str = j.get("started_at")
    if not started_at_str:
        return None
    accrued = j.get("accrued_cost_usd")
    if accrued is None or accrued <= 0:
        return None
    try:
        started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
        elapsed_sec = (now - started_at).total_seconds()
    except (ValueError, TypeError):
        return None
    if elapsed_sec < 60:
        return None
    return (accrued / elapsed_sec) * 3600


def _compute_job_projected_spend(j: dict, burn_rate: "float | None", now: "datetime") -> "float | None":
    """Return projected total spend at the wall-clock deadline given current burn rate; None if unavailable."""
    if burn_rate is None:
        return None
    deadline_str = j.get("wall_clock_deadline")
    if not deadline_str:
        return None
    try:
        deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        remaining_sec = max(0.0, (deadline - now).total_seconds())
    except (ValueError, TypeError):
        return None
    accrued = j.get("accrued_cost_usd") or 0.0
    return accrued + burn_rate * (remaining_sec / 3600)


def cmd_compute(args: argparse.Namespace) -> None:
    """Dispatch compute subcommands: list / show / kill / analyze / run / launch."""
    sub = getattr(args, "compute_cmd", None) or "list"
    if sub == "kill":
        _compute_kill(args)
    elif sub == "analyze":
        _compute_analyze(args)
    elif sub == "show":
        _compute_show(args)
    elif sub == "run":
        _compute_run_local(args)
    elif sub == "launch":
        _compute_launch(args)
    else:
        _compute_list(args)


def _compute_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    limit = getattr(args, "limit", 20)
    status_filter = getattr(args, "status_filter", None)
    if getattr(args, "all_terminal", False):
        resp = api("GET", f"/api/admin/projects/{pid}/compute-jobs"
                   f"?all_terminal=true&limit={limit}")
    else:
        finished = getattr(args, "include_finished", 360)
        resp = api("GET", f"/api/admin/projects/{pid}/compute-jobs"
                   f"?include_finished_minutes={finished}&limit={limit}")
    jobs = resp.get("compute_jobs", []) if isinstance(resp, dict) else resp
    has_more = resp.get("has_more", False) if isinstance(resp, dict) else False
    summary = resp.get("summary") if isinstance(resp, dict) else None
    if status_filter:
        jobs = [j for j in jobs if j.get("status", "").upper() == status_filter.upper()]
    _emit(args, {"compute_jobs": jobs, "has_more": has_more, "summary": summary, "status_filter": status_filter}, _render_compute_list)


def _render_compute_list(data: dict) -> None:
    jobs = data.get("compute_jobs") or []
    print(f"=== COMPUTE JOBS ({len(jobs)}) ===")
    if not jobs:
        print("  (none)")
        return
    now = datetime.now(timezone.utc)
    for j in jobs:
        jid = (j.get("id") or "?")[:8]
        status = j.get("status", "?")
        title = (j.get("task_title") or "")[:50]
        task_type = j.get("task_type") or "?"
        cost = j.get("final_cost_usd")
        cost_str = f"  ${cost:.2f}" if cost is not None else ""
        cv = j.get("criterion_verdict") if isinstance(j.get("criterion_verdict"), dict) else None
        if cv is not None:
            metric = cv.get("metric")
            value = cv.get("value")
            if metric and value is not None:
                verdict_str = f"  met={cv.get('met')} ({metric}={value})"
            else:
                verdict_str = f"  met={cv.get('met')}"
        else:
            verdict_str = ""
        exp = (j.get("experiment") or "")[:20]
        exp_str = f"  exp={exp}" if exp else ""
        burn = _compute_job_burn_rate(j, now)
        burn_str = f"  burn=${burn:.2f}/hr" if burn is not None else ""
        print(f"  {jid}  {status:12} [{task_type}]  {title}{cost_str}{verdict_str}{exp_str}{burn_str}")
    status_filter = data.get("status_filter")
    if data.get("has_more") and not status_filter:
        print("  (more jobs available; increase --limit, --include-finished, or use --all-terminal)")
    if status_filter:
        total = len(jobs)
        active = sum(1 for j in jobs if j.get("status", "").upper() in _ACTIVE_COMPUTE_STATUSES)
        spend = sum(j.get("final_cost_usd") or 0.0 for j in jobs)
        measured = sum(1 for j in jobs if isinstance(j.get("criterion_verdict"), dict) and j["criterion_verdict"].get("measurable"))
        met = sum(1 for j in jobs if isinstance(j.get("criterion_verdict"), dict) and j["criterion_verdict"].get("met"))
        print(f"  --- {total} jobs | {active} active | ${spend:.2f} total spend | {met}/{measured} criteria met ---")
    else:
        summary = data.get("summary") or {}
        if summary:
            total = summary.get("total_jobs", len(jobs))
            active = summary.get("active_jobs", 0)
            spend = summary.get("total_spend_usd", 0.0)
            measured = summary.get("criterion_measured", 0)
            met = summary.get("criterion_met", 0)
            print(f"  --- {total} jobs | {active} active | ${spend:.2f} total spend | {met}/{measured} criteria met ---")


def _compute_kill(args: argparse.Namespace) -> None:
    _require_write_config("compute kill")
    job_id = args.job_id
    if not _confirm(f"Kill compute job {job_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", f"/api/admin/compute-jobs/{job_id}/kill")
    killed_status = (result.get("compute_job") or {}).get("status", "killed")
    _emit(args, result, lambda r: print(f"  {job_id[:8]}: {killed_status}"))


def _compute_show(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    job_id = args.job_id
    # Prefix-match: fetch list and resolve when a short prefix is given.
    # Full IDs have 4 dashes (e.g. cjob-aaa0-0000-0000-000000000001); fewer
    # dashes means the caller supplied a prefix.
    if job_id.count("-") < 4:
        resp = api("GET", f"/api/admin/projects/{pid}/compute-jobs?all_terminal=true&limit=200")
        jobs = resp.get("compute_jobs", []) if isinstance(resp, dict) else resp
        job = next((j for j in jobs if j.get("id", "").startswith(job_id)), None)
        if job is None:
            print(f"No compute job found matching '{job_id}'.")
            return
        full_id = job["id"]
    else:
        full_id = job_id
    resp2 = api("GET", f"/api/admin/compute-jobs/{full_id}")
    job = resp2.get("compute_job") if isinstance(resp2, dict) else None
    if job is None:
        print(f"No compute job found matching '{job_id}'.")
        return
    _emit(args, {"compute_job": job}, _render_compute_show)


def _render_compute_show(data: dict) -> None:
    j = data.get("compute_job") or {}
    print(f"=== COMPUTE JOB: {j.get('id', '?')} ===")
    status = j.get("status", "?")
    print(f"  status    : {status}")
    print(f"  title     : {j.get('task_title') or j.get('title') or '—'}")
    print(f"  type      : {j.get('task_type', '?')}")
    experiment = j.get("experiment")
    if experiment:
        print(f"  experiment: {experiment}")
    hypothesis = j.get("hypothesis")
    if hypothesis:
        print(f"  hypothesis: {hypothesis}")
    status = j.get("status", "")
    if status in _ACTIVE_COMPUTE_STATUSES:
        now = datetime.now(timezone.utc)
        accrued = j.get("accrued_cost_usd")
        if accrued is not None:
            print(f"  accrued   : ${accrued:.4f}")
        burn = _compute_job_burn_rate(j, now)
        if burn is not None:
            print(f"  burn rate : ${burn:.2f}/hr")
            proj = _compute_job_projected_spend(j, burn, now)
            if proj is not None:
                cap = j.get("spend_cap_usd")
                cap_warn = "  ⚠ projected to exceed cap" if (cap is not None and proj > cap) else ""
                print(f"  projected : ${proj:.2f}{cap_warn}")
        cap = j.get("spend_cap_usd")
        if cap is not None:
            print(f"  spend cap : ${cap:.2f}")
        deadline = j.get("wall_clock_deadline")
        if deadline:
            print(f"  deadline  : {deadline}")
    else:
        cost = j.get("final_cost_usd")
        if cost is not None:
            print(f"  cost      : ${cost:.4f}")
        terminal_reason = j.get("terminal_reason")
        if terminal_reason:
            print(f"  terminal  : {terminal_reason}")
        vm_phase = j.get("vm_phase")
        if vm_phase == "provisioning":
            print(f"  vm_phase  : {vm_phase}")
            launch_contract = j.get("launch_contract") or {}
            if launch_contract:
                deadline = launch_contract.get("requested_deadline_seconds")
                model = launch_contract.get("provisioning_model")
                if deadline:
                    print(f"    deadline: {deadline}s")
                if model:
                    print(f"    model   : {model}")
    exit_code = (j.get("polling_metadata") or {}).get("exit_code")
    if exit_code is not None:
        print(f"  exit_code : {exit_code}")
    polling_meta = j.get("polling_metadata") or {}
    command_log = polling_meta.get("command_log")
    if isinstance(command_log, dict):
        text = command_log.get("text")
        if isinstance(text, str) and text.strip():
            print("  Command log:")
            for line in text.split("\n"):
                print(f"    {line}")
    metrics = polling_meta.get("result_metrics") or j.get("result_metrics")
    if metrics:
        arms = metrics.get("arms") if isinstance(metrics, dict) else None
        scalar_items = {k: v for k, v in metrics.items() if k != "arms"} if isinstance(metrics, dict) else {}
        if scalar_items:
            print(f"  metrics   : {scalar_items}")
        if arms:
            print("  per-arm results:")
            _DIAG_KEYS = {"label", "note", "log", "exit_code", "error", "reason", "stderr_excerpt"}
            if isinstance(arms, dict):
                for arm_name, arm_metrics in arms.items():
                    if isinstance(arm_metrics, dict):
                        label = arm_metrics.get("label")
                        note = arm_metrics.get("note")
                        log = arm_metrics.get("log")
                        exit_code = arm_metrics.get("exit_code")
                        error = arm_metrics.get("error") or arm_metrics.get("reason")
                        stderr_excerpt = arm_metrics.get("stderr_excerpt")
                        scalar_metrics = {k: v for k, v in arm_metrics.items() if k not in _DIAG_KEYS}
                        print(f"    [{arm_name}]", end="")
                        if error:
                            print(f" ⚠ FAILED", end="")
                        elif scalar_metrics:
                            print(f" {scalar_metrics}", end="")
                        print()
                        if exit_code is not None:
                            print(f"      exit_code: {exit_code}")
                        if error:
                            print(f"      error: {error}")
                        if stderr_excerpt:
                            print(f"      stderr: {stderr_excerpt[:300]}")
                        if label:
                            print(f"      label: {label}")
                        if note:
                            print(f"      note: {note}")
                        if log:
                            print("      log:")
                            for log_line in log.split("\n"):
                                print(f"        {log_line}")
                    else:
                        print(f"    [{arm_name}] {arm_metrics}")
            elif isinstance(arms, list):
                for i, arm_entry in enumerate(arms):
                    if isinstance(arm_entry, dict):
                        heading = arm_entry.get("label") or arm_entry.get("name") or arm_entry.get("arm") or f"[{i}]"
                        label = arm_entry.get("label")
                        note = arm_entry.get("note")
                        log = arm_entry.get("log")
                        exit_code = arm_entry.get("exit_code")
                        error = arm_entry.get("error") or arm_entry.get("reason")
                        stderr_excerpt = arm_entry.get("stderr_excerpt")
                        scalar_metrics = {k: v for k, v in arm_entry.items() if k not in (_DIAG_KEYS | {"name", "arm"})}
                        print(f"    [{heading}]", end="")
                        if error:
                            print(f" ⚠ FAILED", end="")
                        elif scalar_metrics:
                            print(f" {scalar_metrics}", end="")
                        print()
                        if exit_code is not None:
                            print(f"      exit_code: {exit_code}")
                        if error:
                            print(f"      error: {error}")
                        if stderr_excerpt:
                            print(f"      stderr: {stderr_excerpt[:300]}")
                        if label:
                            print(f"      label: {label}")
                        if note:
                            print(f"      note: {note}")
                        if log:
                            print("      log:")
                            for log_line in log.split("\n"):
                                print(f"        {log_line}")
                    else:
                        print(f"    [{i}] {arm_entry}")
    verdict = j.get("criterion_verdict")
    if isinstance(verdict, dict):
        met = verdict.get("met")
        metric = verdict.get("metric", "")
        value = verdict.get("value")
        threshold = verdict.get("threshold")
        op = verdict.get("operator", "")
        margin_str = ""
        if value is not None and threshold is not None:
            try:
                margin = float(value) - float(threshold)
                margin_str = f"  margin={margin:+.4g}"
            except (TypeError, ValueError):
                pass
        val_str = f"  {metric} {op} {threshold} (got {value})" if metric else ""
        print(f"  criterion : met={met}{val_str}{margin_str}")
    ref = j.get("result_reference")
    if ref:
        print(f"  result_ref: {ref}")
    doc = j.get("knowledge_document_id")
    if doc:
        print(f"  knowledge : {doc}")


def _compute_analyze(args: argparse.Namespace) -> None:
    _require_write_config("compute analyze")
    job_id = args.job_id
    if not _confirm(f"Trigger analysis for compute job {job_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    result = api("POST", f"/api/admin/compute-jobs/{job_id}/analyze")
    _emit(args, result, lambda r: print(
        f"  Analysis complete for {job_id[:8]}: "
        f"outcome={r.get('outcome', '?')}  "
        f"knowledge_doc={r.get('knowledge_document_id', 'none')}"
    ))


_CRITERION_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _coerce_number(value: object) -> float | None:
    """Return value as float when it is a real number (bool excluded), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _find_metric_case_insensitive(metrics: dict, metric: str) -> object:
    """Return the value for metric using case/whitespace-normalized key lookup.

    Returns None when no key matches or multiple distinct keys match (fail closed).
    """
    normalized = metric.strip().lower()
    matches = [(k, v) for k, v in metrics.items() if k.strip().lower() == normalized]
    if len(matches) == 1:
        return matches[0][1]
    return None  # missing or ambiguous


def _evaluate_criterion(metrics: dict | None, criterion: dict) -> dict:
    """Evaluate a success criterion against captured metrics.

    Returns a dict with keys: verdict ("MET"|"NOT MET"|"NOT EVALUABLE"),
    metric, operator, threshold, value (float|None), met (bool).
    Fail-closed: any missing/invalid/ambiguous input yields NOT EVALUABLE + met=False.
    """
    metric_name = criterion.get("metric")
    op = criterion.get("operator")
    threshold = criterion.get("threshold")

    base = {"metric": metric_name, "operator": op, "threshold": threshold,
            "value": None, "met": False}

    fn = _CRITERION_OPS.get(op) if op else None
    thr = _coerce_number(threshold)

    if not metric_name or fn is None or thr is None:
        return {"verdict": "NOT EVALUABLE", **base}

    if metrics is None:
        return {"verdict": "NOT EVALUABLE", **base}

    raw_value = _find_metric_case_insensitive(metrics, metric_name)
    value = _coerce_number(raw_value)
    if value is None:
        return {"verdict": "NOT EVALUABLE", **base}

    met = fn(value, thr)
    verdict = "MET" if met else "NOT MET"
    return {"verdict": verdict, "metric": metric_name, "operator": op,
            "threshold": thr, "value": value, "met": met}


def _detect_local_gpu() -> dict:
    """Probe for a local GPU using nvidia-smi or rocm-smi.

    Guarded by shutil.which so no subprocess is spawned in environments where
    the tools are absent (e.g., CI/test VMs). Fail-soft by contract — never
    raises; returns detected=False on any error.

    Returns a dict with keys:
      detected (bool), backend (str|None), devices (list[str])
    """
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                devices = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
                return {"detected": True, "backend": "nvidia", "devices": devices}
        except Exception:  # noqa: BLE001
            pass

    if shutil.which("rocm-smi"):
        try:
            result = subprocess.run(
                ["rocm-smi", "--showproductname"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                devices = [line.strip() for line in result.stdout.strip().splitlines()
                           if line.strip() and "GPU" in line]
                if devices:
                    return {"detected": True, "backend": "rocm", "devices": devices}
        except Exception:  # noqa: BLE001
            pass

    return {"detected": False, "backend": None, "devices": []}


_COMPUTE_STDERR_MAX_BYTES = 2048


def _compute_arm_failure_reason(
    exit_code: int | None,
    declared_metric: str | None,
    *,
    result_missing: bool,
    metric_missing: bool,
) -> str:
    if exit_code == 124:
        return "arm timed out (exit 124)"
    if result_missing:
        if exit_code is not None and exit_code != 0:
            return f"arm crashed (exit {exit_code}): result file not written"
        return "arm result file not written (arm may have failed)"
    if metric_missing:
        metric_name = declared_metric or "<unknown>"
        if exit_code is not None and exit_code != 0:
            return f"arm crashed (exit {exit_code}): metric '{metric_name}' not found in result"
        return f"unmeasured criterion: metric '{metric_name}' not found in result"
    if exit_code is not None and exit_code != 0:
        return f"arm crashed (exit {exit_code})"
    return "unknown failure"


def _read_arm_stderr_excerpt(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            return None
        tail = data[-_COMPUTE_STDERR_MAX_BYTES:]
        return tail.decode(errors="replace").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _compute_docker_login(auth: dict, *, _subprocess_run=None) -> bool:
    """Run `docker login` using the credentials in *auth*.

    Gates on ``password`` only — server and username are optional.
    Returns True on success or when no password is present; False on any
    failure (non-zero exit, timeout, subprocess error).

    ``_subprocess_run`` is an optional override injected by tests.
    """
    password = auth.get("password") or ""
    if not password:
        return True

    server = auth.get("server") or ""
    username = auth.get("username") or ""
    _run = _subprocess_run if _subprocess_run is not None else subprocess.run

    cmd = ["docker", "login", "--password-stdin"]
    if username:
        cmd += ["--username", username]
    if server:
        cmd.append(server)

    try:
        result = _run(cmd, input=password.encode(), capture_output=True, timeout=60)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace")[:500]
            print(f"error: docker login failed (exit={result.returncode}): {stderr}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("error: docker login timed out")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"error: docker login error: {exc}")
        return False


def _compute_run_local(args: argparse.Namespace) -> None:
    """Execute a compute_experiment command locally without the orchestrator.

    Sets BLUEPRINT_RESULT_FILE and BLUEPRINT_ARTIFACT_DIR so the command can
    emit metrics and artifacts via the standard file-contract, then reports the
    result inline.
    """
    declared_metric: str | None = None
    success_criterion: dict | None = None
    arms: list | None = None
    container_image: str | None = None
    container_registry_auth: dict | None = None
    spec_env: dict[str, str] | None = None
    spec_cwd: str | None = None
    spec_repo_clone: dict | None = None
    if getattr(args, "local_spec", None):
        # --local <spec.json>: read spec file; validate_backend_name is bypassed
        # because we call subprocess.run directly (no DB-backed flow).
        if getattr(args, "run_command", None):
            print("warning: both a positional command and --local were given; --local takes precedence")
        try:
            with open(args.local_spec) as f:
                spec = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"error: could not read spec file {args.local_spec!r}: {exc}")
            sys.exit(1)
        arms = spec.get("arms") or None
        command = spec.get("command")
        if not command and not arms:
            print("error: spec JSON must contain a 'command' field (or an 'arms' list)")
            sys.exit(1)
        declared_metric = spec.get("declared_metric")
        raw_sc = spec.get("success_criterion")
        if isinstance(raw_sc, dict):
            success_criterion = raw_sc
        elif raw_sc is not None:
            print("error: spec 'success_criterion' must be an object with metric/operator/threshold")
            sys.exit(1)
        container_image = spec.get("container_image") or None
        raw_auth = spec.get("container_registry_auth")
        if isinstance(raw_auth, dict):
            container_registry_auth = raw_auth
        raw_env = spec.get("env")
        if isinstance(raw_env, dict):
            spec_env = {str(k): str(v) for k, v in raw_env.items()}
        raw_cwd = spec.get("cwd")
        if isinstance(raw_cwd, str) and raw_cwd:
            spec_cwd = raw_cwd
        raw_repo_clone = spec.get("repo_clone")
        if isinstance(raw_repo_clone, dict):
            spec_repo_clone = raw_repo_clone
    else:
        command = args.run_command
        container_image = getattr(args, "container_image", None) or None

    # CLI flags as fallback criterion source (spec takes precedence)
    if success_criterion is None:
        cm = getattr(args, "criterion_metric", None)
        co = getattr(args, "criterion_op", None)
        ct = getattr(args, "criterion_threshold", None)
        if cm is not None and co is not None and ct is not None:
            success_criterion = {"metric": cm, "operator": co, "threshold": ct}

    if command is None and not arms:
        print("error: provide a run command or --local <spec.json>")
        sys.exit(1)

    tmpdir = tempfile.mkdtemp(prefix="blueprint-compute-run-")
    artifact_dir = os.path.join(tmpdir, "artifacts")
    os.makedirs(artifact_dir, exist_ok=True)
    env = os.environ.copy()
    env["BLUEPRINT_ARTIFACT_DIR"] = artifact_dir

    gpu_info = _detect_local_gpu()
    if gpu_info["detected"]:
        device_label = gpu_info["devices"][0] if gpu_info["devices"] else "unknown"
        env["BLUEPRINT_LOCAL_GPU"] = device_label
    else:
        env["BLUEPRINT_LOCAL_GPU"] = "none"

    if container_image:
        if container_registry_auth and isinstance(container_registry_auth, dict):
            if container_registry_auth.get("password"):
                if not _compute_docker_login(container_registry_auth):
                    sys.exit(1)
        if not _json_mode(args):
            print(f"image       : {container_image}")
            print("docker pull : pulling...")
        pull_result = subprocess.run(
            ["docker", "pull", container_image],
            stdout=subprocess.DEVNULL if _json_mode(args) else None,
        )
        if pull_result.returncode != 0:
            print(f"error: docker pull {container_image!r} failed (exit {pull_result.returncode})")
            sys.exit(pull_result.returncode)

    if arms:
        arm_names = [a.get("name", f"arm_{i}") for i, a in enumerate(arms)]
        if not _json_mode(args):
            print(f"arms        : {arm_names}")
            if gpu_info["detected"]:
                print(f"gpu         : {env['BLUEPRINT_LOCAL_GPU']}")
            else:
                print("gpu         : none")
            if declared_metric:
                print(f"declared    : {declared_metric}")
            print("running arms...")

        arm_results: dict[str, dict] = {}
        overall_exit_code = 0
        for i, arm in enumerate(arms):
            arm_name = arm.get("name", f"arm_{i}")
            arm_cmd = arm.get("command", "")
            arm_result_file = os.path.join(tmpdir, f"result_{arm_name}.json")
            arm_stderr_file = os.path.join(tmpdir, f"stderr_{arm_name}.txt")
            arm_env = env.copy()
            arm_env["BLUEPRINT_RESULT_FILE"] = arm_result_file

            if not _json_mode(args):
                print(f"  [{arm_name}] running...")
            with open(arm_stderr_file, "wb") as arm_stderr_fh:
                if container_image:
                    container_result = f"/tmp/blueprint_out/result_{arm_name}.json"
                    docker_arm_cmd = ["docker", "run", "--rm"]
                    if gpu_info["detected"]:
                        docker_arm_cmd += ["--gpus", "all"]
                    if spec_repo_clone:
                        _repo_root = os.path.abspath(".")
                        docker_arm_cmd += ["-v", f"{_repo_root}:/workspace"]
                        docker_arm_cmd += ["-e", "PYTHONPATH=/workspace"]
                        docker_arm_cmd += ["-w", "/workspace"]
                    elif spec_cwd:
                        docker_arm_cmd += ["-w", spec_cwd]
                    for _k, _v in (spec_env or {}).items():
                        docker_arm_cmd += ["-e", f"{_k}={_v}"]
                    docker_arm_cmd += [
                        "-v", f"{tmpdir}:/tmp/blueprint_out",
                        "-e", f"BLUEPRINT_RESULT_FILE={container_result}",
                        "-e", "BLUEPRINT_ARTIFACT_DIR=/tmp/blueprint_out/artifacts",
                        container_image, "bash", "-lc", arm_cmd,
                    ]
                    arm_proc = subprocess.run(docker_arm_cmd, stderr=arm_stderr_fh)
                else:
                    arm_env.update(spec_env or {})
                    arm_proc = subprocess.run(
                        ["bash", "-lc", arm_cmd], env=arm_env, cwd=spec_cwd or None,
                        stderr=arm_stderr_fh,
                    )
            arm_exit_code = arm_proc.returncode
            if arm_exit_code != 0:
                overall_exit_code = arm_exit_code

            # Parse result file
            result_missing = not os.path.exists(arm_result_file)
            arm_metrics: dict | None = None
            if not result_missing:
                try:
                    with open(arm_result_file) as f:
                        arm_metrics = json.load(f)
                except Exception as exc:  # noqa: BLE001
                    if not _json_mode(args):
                        print(f"  [{arm_name}] warning: could not parse result file: {exc}")
                    result_missing = True

            metric_missing = (
                declared_metric is not None
                and not result_missing
                and arm_metrics is not None
                and arm_metrics.get(declared_metric) is None
            )

            # Build canonical per-arm entry
            is_failure = arm_exit_code != 0 or result_missing or metric_missing
            if is_failure:
                reason = _compute_arm_failure_reason(
                    arm_exit_code,
                    declared_metric,
                    result_missing=result_missing,
                    metric_missing=metric_missing,
                )
                arm_entry: dict = {"error": reason, "exit_code": arm_exit_code}
                excerpt = _read_arm_stderr_excerpt(arm_stderr_file)
                if excerpt:
                    arm_entry["stderr_excerpt"] = excerpt
                arm_results[arm_name] = arm_entry
            elif declared_metric and arm_metrics is not None:
                arm_results[arm_name] = {declared_metric: arm_metrics[declared_metric], "exit_code": arm_exit_code}
            else:
                arm_entry = arm_metrics if arm_metrics is not None else {}
                arm_entry["exit_code"] = arm_exit_code
                arm_results[arm_name] = arm_entry

        status = "SUCCEEDED" if overall_exit_code == 0 else "FAILED"
        metrics: dict | None = {"arms": arm_results}
        verdict: dict | None = None
        result = {"status": status, "exit_code": overall_exit_code, "metrics": metrics,
                  "declared_metric": declared_metric, "verdict": verdict, "gpu": gpu_info}

        def _render(r: dict) -> None:
            print(f"status      : {r['status']} (exit_code={r['exit_code']})")
            dm = r.get("declared_metric")
            arms_block = (r.get("metrics") or {}).get("arms", {})
            if arms_block:
                print(f"arms        : {len(arms_block)} arm(s)")
                for aname, ametrics in arms_block.items():
                    error = ametrics.get("error")
                    if error:
                        print(f"  [{aname}] FAILED: {error}")
                        excerpt = ametrics.get("stderr_excerpt")
                        if excerpt:
                            print(f"  [{aname}] stderr: {excerpt[:200]}")
                    elif dm:
                        val = ametrics.get(dm)
                        if val is not None:
                            print(f"  [{aname}] {dm}={val}")
                        else:
                            print(f"  [{aname}] {dm}=NOT CAPTURED")
                    else:
                        print(f"  [{aname}] {ametrics}")
            else:
                print("metrics     : (none emitted)")

        _emit(args, result, _render)
        return

    result_file = os.path.join(tmpdir, "result.json")
    env["BLUEPRINT_RESULT_FILE"] = result_file

    if not _json_mode(args):
        print(f"command     : {command}")
        print(f"result_file : {result_file}")
        if gpu_info["detected"]:
            print(f"gpu         : {env['BLUEPRINT_LOCAL_GPU']}")
        else:
            print("gpu         : none")
        if declared_metric:
            print(f"declared    : {declared_metric}")
    if not _json_mode(args) and success_criterion:
        sc = success_criterion
        print(f"criterion   : {sc.get('metric')} {sc.get('operator')} {sc.get('threshold')}")
    if not _json_mode(args):
        print("running...")

    if container_image:
        docker_single_cmd = ["docker", "run", "--rm"]
        if gpu_info["detected"]:
            docker_single_cmd += ["--gpus", "all"]
        if spec_repo_clone:
            _repo_root = os.path.abspath(".")
            docker_single_cmd += ["-v", f"{_repo_root}:/workspace"]
            docker_single_cmd += ["-e", "PYTHONPATH=/workspace"]
            docker_single_cmd += ["-w", "/workspace"]
        elif spec_cwd:
            docker_single_cmd += ["-w", spec_cwd]
        for _k, _v in (spec_env or {}).items():
            docker_single_cmd += ["-e", f"{_k}={_v}"]
        docker_single_cmd += [
            "-v", f"{tmpdir}:/tmp/blueprint_out",
            "-e", "BLUEPRINT_RESULT_FILE=/tmp/blueprint_out/result.json",
            "-e", "BLUEPRINT_ARTIFACT_DIR=/tmp/blueprint_out/artifacts",
            container_image, "bash", "-lc", command,
        ]
        proc = subprocess.run(docker_single_cmd)
        result_file = os.path.join(tmpdir, "result.json")
    else:
        env.update(spec_env or {})
        proc = subprocess.run(["bash", "-lc", command], env=env, cwd=spec_cwd or None)
    exit_code = proc.returncode
    status = "SUCCEEDED" if exit_code == 0 else "FAILED"

    metrics = None
    if os.path.exists(result_file):
        try:
            with open(result_file) as f:
                metrics = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"  warning: could not parse result file: {exc}")

    verdict = None
    if success_criterion is not None:
        verdict = _evaluate_criterion(metrics, success_criterion)

    result = {"status": status, "exit_code": exit_code, "metrics": metrics,
              "declared_metric": declared_metric, "verdict": verdict, "gpu": gpu_info}

    def _render(r: dict) -> None:
        print(f"status      : {r['status']} (exit_code={r['exit_code']})")
        m = r.get("metrics")
        dm = r.get("declared_metric")
        if m:
            print(f"metrics     : {m}")
            if dm:
                val = m.get(dm)
                if val is not None:
                    print(f"  {dm} (declared) : {val}")
                else:
                    print(f"  {dm} (declared) : NOT CAPTURED")
        else:
            print("metrics     : (none emitted)")
        v = r.get("verdict")
        if v:
            vstr = v["verdict"]
            val = v.get("value")
            op = v.get("operator")
            thr = v.get("threshold")
            mn = v.get("metric")
            if val is not None:
                print(f"verdict     : {vstr}  ({mn}={val} {op} {thr})")
            else:
                print(f"verdict     : {vstr}  ({mn} not evaluable)")

    _emit(args, result, _render)


# No-repo substrate probe command (inlined from launch_compute_experiment.py).
_COMPUTE_DUD_COMMAND = """python3 - <<'PYEOF'
import json
import os
import sys

result = {"dud_ok": 1, "py_major": sys.version_info[0]}
rf = os.environ.get("BLUEPRINT_RESULT_FILE")
if rf:
    with open(rf, "w") as f:
        json.dump(result, f)
ad = os.environ.get("BLUEPRINT_ARTIFACT_DIR")
if ad:
    with open(os.path.join(ad, "dud.txt"), "w") as f:
        f.write("compute substrate reached runtime and wrote an artifact\\n")
print(json.dumps(result))
PYEOF"""


def _compute_launch(args: argparse.Namespace) -> None:
    _require_write_config("compute launch")
    dud = getattr(args, "dud", False)
    command = _COMPUTE_DUD_COMMAND if dud else getattr(args, "experiment_command", None)
    if not command:
        print("ERROR: provide --dud or --command.", file=sys.stderr)
        sys.exit(1)
    cfg = load_config()
    github_repo = getattr(args, "repo", None) or cfg.get("default_repo", "")
    if not github_repo:
        print("ERROR: --repo is required (or set default_repo in config).", file=sys.stderr)
        sys.exit(1)
    title = getattr(args, "title", None) or ("operator substrate probe" if dud else "operator compute experiment")
    prompt = getattr(args, "prompt", None) or "Operator-launched compute_experiment."
    base_branch = getattr(args, "base_branch", None) or cfg.get("default_base_branch", "main")
    spend_cap = getattr(args, "spend_cap", 0.50)
    deadline = getattr(args, "deadline", 600)
    raw_metric = getattr(args, "metric", None)
    metric = (raw_metric or "dud_ok") if dud else raw_metric
    operator = getattr(args, "operator", ">=")
    raw_threshold = getattr(args, "threshold", None)
    threshold = (raw_threshold if raw_threshold is not None else 1.0) if dud else raw_threshold
    if not _confirm(f"Launch compute experiment: {title[:60]}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body: dict = {
        "github_repo": github_repo,
        "title": title,
        "prompt": prompt,
        "base_branch": base_branch,
        "command": command,
        "spend_cap_usd": spend_cap,
        "deadline_seconds": deadline,
    }
    if metric:
        body["success_criterion"] = {
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
        }
    if getattr(args, "machine_type", None):
        body["machine_type"] = args.machine_type
    result = api("POST", "/api/admin/tasks/compute-experiment", body)
    _emit(args, result, lambda r: print(
        f"  compute experiment created: {r.get('task_id', r.get('id', '?'))}"
        + (f"  job_id={r.get('compute_job_id', '')}" if r.get("compute_job_id") else "")
    ))


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
    # default=SUPPRESS (like --json) so an absent flag on a subparser never
    # clobbers a value set on the top-level parser — the classic argparse
    # parent/subparser override gotcha. With default=None this flag was silently
    # dropped when placed *before* the subcommand.
    common.add_argument("--project-id", dest="project_id", default=argparse.SUPPRESS,
                        help="Target project by id (overrides --project, "
                             "BLUEPRINT_PROJECT_ID, and config).")
    common.add_argument("--project", dest="project_name", default=argparse.SUPPRESS,
                        help="Target project by case-insensitive name match "
                             "(resolved via GET /api/admin/projects).")

    parser = argparse.ArgumentParser(
        prog="blueprint",
        description="Unified Blueprint operator CLI (status/diagnostics/tasks/"
                    "versions/promote-recover/logs).",
        epilog="Auth: set BLUEPRINT_API_KEY + BLUEPRINT_API_URL env vars, or place "
               "~/.blueprint-config.json (keys: api_url, api_key), or run "
               "`gcloud auth login` to auto-bootstrap.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    # projects
    sub.add_parser("projects", parents=[common],
                   help="List all projects (id, name, status).")

    # status
    sub.add_parser("status", parents=[common],
                   help="Version phase, deploy state, and promotion eligibility.")

    # diagnostics
    sub.add_parser("diagnostics", parents=[common],
                   help="Stuck tasks, circuit breakers, and grouped failure modes.")

    # ops
    p_ops = sub.add_parser("ops", parents=[common],
                           help="System intervention: repair state, health reset, requeue, restart.",
                           description="System intervention subcommands for stuck-state recovery, "
                                       "health resets, task requeuing, and orchestrator restarts.")
    ops_sub = p_ops.add_subparsers(dest="ops_cmd")
    p_ops_repair = ops_sub.add_parser("repair", parents=[common],
                       help="Repair stuck state (orphaned parents, stale locks).")
    p_ops_repair.add_argument("--yes", action="store_true",
                              help="Skip the confirmation prompt (for scripting).")
    p_ops_reset = ops_sub.add_parser("reset-health", parents=[common],
                       help="Force-reset health checks.")
    p_ops_reset.add_argument("--yes", action="store_true",
                             help="Skip the confirmation prompt (for scripting).")
    p_ops_requeue = ops_sub.add_parser("requeue-failed", parents=[common],
                       help="Requeue all FAILED tasks.")
    p_ops_requeue.add_argument("--yes", action="store_true",
                               help="Skip the confirmation prompt (for scripting).")
    p_ops_restart = ops_sub.add_parser("restart-orchestrator", parents=[common],
                       help="Restart background loops.")
    p_ops_restart.add_argument("--yes", action="store_true",
                               help="Skip the confirmation prompt (for scripting).")
    p_ops_cb = ops_sub.add_parser("circuit-breaker", parents=[common],
                                  help="Show circuit breaker state or reset for a project.")
    p_ops_cb.add_argument("--reset", metavar="PROJECT_ID", default=None,
                          help="Reset circuit breaker for a project (defaults to current project).")
    p_ops_cb.add_argument("--yes", action="store_true",
                          help="Skip the confirmation prompt (for scripting).")

    # trials
    sub.add_parser("trials", parents=[common],
                   help="Capability-trial convergence status, arm counts, and days-to-decision.")

    # tasks
    p_tasks = sub.add_parser("tasks", parents=[common], help="List or show tasks.",
                             description="List, filter, show, cancel, or requeue tasks. "
                                         "Defaults to `list` when no subcommand is given.")
    tasks_sub = p_tasks.add_subparsers(dest="tasks_cmd")
    p_tasks_list = tasks_sub.add_parser("list", parents=[common], help="List/filter tasks.")
    p_tasks_list.add_argument("--status", help="Filter by task status (e.g. IN_PROGRESS).")
    p_tasks_list.add_argument("--type", dest="task_type", help="Filter by task type.")
    p_tasks_list.add_argument("--direction", help="Filter by direction id.")
    p_tasks_list.add_argument("--search", help="Substring search over title/desc/message.")
    p_tasks_list.add_argument("--limit", type=int, default=50, help="Max tasks (default 50).")
    p_tasks_list.add_argument("--offset", type=int, default=0,
                              help="Skip the first N tasks (server-side pagination; "
                                   "combine with --limit to page through results).")
    p_tasks_list.add_argument("--blocked", action="store_true",
                              help="Show only tasks in BLOCKED status (dependency "
                                   "unresolved). Appends BLOCKED to any --status values.")
    p_tasks_list.add_argument("--approval-needed", dest="approval_needed",
                              action="store_true",
                              help="Show only tasks that require human approval.")
    p_tasks_list.add_argument("--failure-class", dest="failure_class", action="append",
                              default=[], choices=sorted(FAILURE_CLASSES), metavar="CLASS",
                              help="Keep only tasks whose failure_class matches (repeatable). "
                                   "Filters CLIENT-SIDE after fetching (matches the web "
                                   "console); raises the internal fetch limit to 500 so "
                                   "filtering has enough rows. Only FAILED/CANCELLED tasks "
                                   "carry a failure_class.")
    p_tasks_list.add_argument("--version", help="UNSUPPORTED by the API — prints a notice "
                                                "(Task has no release-version linkage).")
    p_tasks_show = tasks_sub.add_parser("show", parents=[common], help="Show one task by id.")
    p_tasks_show.add_argument("task_id", help="Task id (full uuid).")
    p_tasks_cancel = tasks_sub.add_parser("cancel", parents=[common],
                                          help="Cancel a task by id (confirms before mutating).")
    p_tasks_cancel.add_argument("task_id", help="Task id (full uuid).")
    p_tasks_cancel.add_argument("--yes", action="store_true",
                                help="Skip the confirmation prompt (for scripting).")
    p_tasks_requeue = tasks_sub.add_parser("requeue", parents=[common],
                                           help="Requeue a task by id (confirms before mutating).")
    p_tasks_requeue.add_argument("task_id", help="Task id (full uuid).")
    p_tasks_requeue.add_argument("--yes", action="store_true",
                                 help="Skip the confirmation prompt (for scripting).")
    p_tasks_requeue.add_argument(
        "--prompt-suffix",
        "--guidance",
        dest="requeue_prompt_suffix",
        help=(
            "Operator guidance to append to the payload delivered on the retry. "
            "Replaces guidance from an earlier manual retry without changing "
            "the original task prompt."
        ),
    )
    p_tasks_scope = tasks_sub.add_parser("scope", parents=[common],
                                         help="Show prompt, research question, and payload scope for a task.")
    p_tasks_scope.add_argument("task_id", help="Task id (full uuid).")
    p_tasks_scope.add_argument("-v", "--verbose", action="store_true",
                               help="Show full prompt text (default truncates at 500 chars).")
    p_tasks_tree = tasks_sub.add_parser("tree", parents=[common],
                                        help="Show a parent task and its children with status/PR/merge state.")
    p_tasks_tree.add_argument("parent_id", help="Parent task id (full uuid).")
    p_tasks_tree.add_argument("--all", action="store_true",
                              help="Include review/fix/debug children (hidden by default).")
    p_tasks_failures = tasks_sub.add_parser(
        "failures", parents=[common],
        help="Show recent FAILED tasks grouped by root cause for incident triage.")
    p_tasks_failures.add_argument("--limit", type=int, default=50,
                                  help="Max failed tasks to fetch (default 50).")
    p_tasks_evaluating = tasks_sub.add_parser(
        "evaluating", parents=[common],
        help="Show all EVALUATING tasks with CI/PR state.")
    p_tasks_evaluating.add_argument("-v", "--verbose", action="store_true",
                                    help="Show research question and scope.")
    p_tasks_cancel_all = tasks_sub.add_parser(
        "cancel-all", parents=[common],
        help="Cancel tasks in bulk (confirms before mutating).")
    p_tasks_cancel_all.add_argument("--status", help="Filter by status (default: non-terminal).")
    p_tasks_cancel_all.add_argument("--category",
                                    help="Filter by context.failure_category.")
    p_tasks_cancel_all.add_argument("--yes", action="store_true",
                                    help="Skip the confirmation prompt.")
    p_tasks_requeue_ci = tasks_sub.add_parser(
        "requeue-ci", parents=[common],
        help="Requeue all CI-waiting EVALUATING tasks (confirms before mutating).")
    p_tasks_requeue_ci.add_argument("--yes", action="store_true",
                                    help="Skip the confirmation prompt.")
    p_tasks_trigger_rollup = tasks_sub.add_parser(
        "trigger-rollup", parents=[common],
        help="Force rollup PR creation for a stuck parent task (confirms before mutating).")
    p_tasks_trigger_rollup.add_argument("parent_id", help="Parent task id (full uuid).")
    p_tasks_trigger_rollup.add_argument("--yes", action="store_true",
                                        help="Skip the confirmation prompt.")
    p_tasks_complete = tasks_sub.add_parser(
        "complete", parents=[common],
        help="Mark a task COMPLETED (confirms before mutating).")
    p_tasks_complete.add_argument("task_id", help="Task id.")
    p_tasks_complete.add_argument("--message", "-m", help="Status message to record.")
    p_tasks_complete.add_argument("--yes", action="store_true",
                                  help="Skip the confirmation prompt.")
    p_tasks_set_status = tasks_sub.add_parser(
        "set-status", parents=[common],
        help="Set a task to an arbitrary status (confirms before mutating).")
    p_tasks_set_status.add_argument("task_id", help="Task id.")
    p_tasks_set_status.add_argument(
        "new_status",
        choices=["QUEUED", "PROVISIONING", "IN_PROGRESS",
                 "EVALUATING", "COMPLETED", "FAILED", "CANCELLED"],
        help="Target status.")
    p_tasks_set_status.add_argument("--message", "-m", help="Status message to record.")
    p_tasks_set_status.add_argument("--yes", action="store_true",
                                    help="Skip the confirmation prompt.")
    p_tasks_reset_cap = tasks_sub.add_parser(
        "reset-iteration-cap", parents=[common],
        help="Reset the review-fix iteration cap for a stuck task (confirms before mutating).")
    p_tasks_reset_cap.add_argument("task_id", help="Task id.")
    p_tasks_reset_cap.add_argument("--yes", action="store_true",
                                   help="Skip the confirmation prompt.")
    p_tasks_archive = tasks_sub.add_parser(
        "archive", parents=[common],
        help="Archive old terminal tasks (confirms before mutating).")
    p_tasks_archive.add_argument("--completed", action="store_true",
                                 help="Archive only COMPLETED tasks (not FAILED/CANCELLED).")
    p_tasks_archive.add_argument("--pipelines", action="store_true",
                                 help="Archive fully-terminal pipelines (parent+all children).")
    p_tasks_archive.add_argument("--yes", action="store_true",
                                 help="Skip the confirmation prompt.")
    p_tasks_research = tasks_sub.add_parser(
        "research", parents=[common],
        help="Show recent completed research tasks.")
    p_tasks_research.add_argument("task_id", nargs="?", default=None,
                                  help="Research task id (show detail; default lists recent).")
    p_tasks_research.add_argument("--limit", type=int, default=5,
                                  help="Max tasks to show (default 5).")
    p_tasks_research.add_argument("-v", "--verbose", action="store_true",
                                  help="Verbose output.")
    tasks_sub.add_parser(
        "autonomy-debug", parents=[common],
        help="Show why autonomy is or isn't spawning: blocking guards and eligible directions.")
    p_tasks_create = tasks_sub.add_parser(
        "create", parents=[common],
        help="Create and queue a new task (confirms before mutating).")
    p_tasks_create.add_argument("title", help="Short task title.")
    p_tasks_create.add_argument("prompt", help="Full task prompt / description.")
    p_tasks_create.add_argument(
        "--type", dest="task_type", default="create",
        choices=["create", "fix", "research", "memo", "design_doc_update"],
        help="Task type (default: create).")
    p_tasks_create.add_argument(
        "--repo", default=None, metavar="OWNER/REPO",
        help="GitHub repo (default: default_repo from config).")
    p_tasks_create.add_argument(
        "--base-branch", dest="base_branch", default=None,
        help="Base branch (default: default_base_branch from config or 'main').")
    p_tasks_create.add_argument(
        "--model", default=None, help="Model override for this task.")
    p_tasks_create.add_argument(
        "--effort", default=None,
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Effort/reasoning level override.")
    p_tasks_create.add_argument(
        "--hotfix", action="store_true", help="Mark task as hotfix priority.")
    p_tasks_create.add_argument("--yes", action="store_true",
                                help="Skip the confirmation prompt.")

    # versions
    p_versions = sub.add_parser("versions", parents=[common],
                                help="List release versions or show a summary.",
                                description="List release versions or show a detailed summary "
                                            "for a specific version.")
    versions_sub = p_versions.add_subparsers(dest="versions_cmd")
    p_versions_list = versions_sub.add_parser(
        "list", parents=[common],
        help="List current + recent versions (or browse full history with --all/--limit).")
    p_versions_list.add_argument(
        "--all", action="store_true", dest="all_versions",
        help="Browse the full version history (paginated) instead of current + recent.")
    p_versions_list.add_argument(
        "--limit", type=int, default=None,
        help="Page size for history browsing (1-100). Implies the full-history view.")
    p_versions_list.add_argument(
        "--offset", type=int, default=0,
        help="Rows to skip for history browsing (use with --limit).")
    p_versions_show = versions_sub.add_parser("show", parents=[common],
                                              help="Show a version summary (current by default).")
    p_versions_show.add_argument("version_number", nargs="?", default=None,
                                 help="Version number (defaults to the open version).")
    p_versions_export = versions_sub.add_parser(
        "export-changelog", parents=[common],
        help="Export the version changelog as Markdown (stdout or --out FILE).")
    p_versions_export.add_argument("version_number", nargs="?", default=None,
                                   help="Version number (defaults to the open version).")
    p_versions_export.add_argument("--out", metavar="FILE", default=None,
                                   help="Write output to FILE instead of stdout.")

    # system
    p_system = sub.add_parser(
        "system", parents=[common],
        help="Break-glass system ops: repair, restart, zones, autonomy.",
        description="System-level operator commands. Repair stuck state, restart the "
                    "orchestrator, inspect zone health, or view autonomy state.")
    system_sub = p_system.add_subparsers(dest="system_cmd")
    p_sys_repair = system_sub.add_parser("repair", parents=[common],
                                         help="Repair stuck/orphaned state (confirms before mutating).")
    p_sys_repair.add_argument("--yes", action="store_true", help="Skip confirmation.")
    p_sys_restart = system_sub.add_parser("restart", parents=[common],
                                          help="Restart orchestrator background loops (confirms).")
    p_sys_restart.add_argument("--yes", action="store_true", help="Skip confirmation.")
    system_sub.add_parser("zones", parents=[common], help="Show zone health.")
    system_sub.add_parser("autonomy", parents=[common],
                          help="Show autonomous research state per project.")

    # monitor
    p_monitor = sub.add_parser(
        "monitor", parents=[common],
        help="Situational awareness: alerts, digest, activity feed.",
        description="Monitor system state: show active alerts, catch-up digest, or "
                    "recent activity feed for operational awareness.")
    monitor_sub = p_monitor.add_subparsers(dest="monitor_cmd")
    monitor_sub.add_parser("alerts", parents=[common], help="Show active system alerts.")
    p_mon_digest = monitor_sub.add_parser("digest", parents=[common],
                                          help="Catch-up digest (last N hours, default 24).")
    p_mon_digest.add_argument("--hours", type=int, default=24, help="Lookback window in hours.")
    p_mon_activity = monitor_sub.add_parser("activity", parents=[common],
                                            help="Recent activity feed (completions, failures, research).")
    p_mon_activity.add_argument("--limit", type=int, default=20,
                               help="Max activity events to show (default 20).")

    # directions
    p_dirs = sub.add_parser(
        "directions", parents=[common],
        help="View and steer research directions.",
        description="View and steer research directions. Defaults to `list` "
                    "when no subcommand is given.")
    dirs_sub = p_dirs.add_subparsers(dest="directions_cmd")

    p_dirs_list = dirs_sub.add_parser("list", parents=[common],
                                      help="List active/proposed directions.")
    p_dirs_list.add_argument("--all", action="store_true",
                             help="Include paused/completed/expired directions.")
    p_dirs_list.add_argument("--staged", action="store_true",
                             help="Show only staged directions.")

    p_dirs_show = dirs_sub.add_parser("show", parents=[common],
                                      help="Show one direction in detail.")
    p_dirs_show.add_argument("direction_id", help="Direction id or prefix.")

    p_dirs_add = dirs_sub.add_parser("add", parents=[common],
                                     help="Add a new direction (confirms before posting).")
    p_dirs_add.add_argument("text", help="Direction text.")
    p_dirs_add.add_argument("--priority", choices=["high", "medium", "low"],
                            default="medium", help="Priority (default medium).")
    p_dirs_add.add_argument("--paths", nargs="+", metavar="PATH",
                            help="File paths associated with this direction.")
    p_dirs_add.add_argument("--completion-criteria", dest="completion_criteria",
                            metavar="TEXT", help="Completion criteria text.")
    p_dirs_add.add_argument("--staged", action="store_true",
                            help="Create the direction staged against the open version.")
    p_dirs_add.add_argument("--yes", action="store_true",
                            help="Skip the confirmation prompt.")

    p_dirs_update = dirs_sub.add_parser("update", parents=[common],
                                        help="Update a direction (confirms before patching).")
    p_dirs_update.add_argument("direction_id", help="Direction id or prefix.")
    p_dirs_update.add_argument("--text", help="New direction text.")
    p_dirs_update.add_argument("--priority", choices=["high", "medium", "low"],
                               help="New priority.")
    p_dirs_update.add_argument("--status",
                               choices=["active", "paused", "completed",
                                        "staged", "proposed", "expired"],
                               help="New status.")
    p_dirs_update.add_argument("--staged-for-version", dest="staged_for_version",
                               type=int, metavar="INT",
                               help="Retarget a staged direction to version INT.")
    p_dirs_update.add_argument("--reset-context", dest="reset_context",
                               action="store_true",
                               help="Wipe research_context back to blank template.")
    p_dirs_update.add_argument("--reset-history", dest="reset_history",
                               action="store_true",
                               help="Wipe research_history (sets to []).")
    p_dirs_update.add_argument("--reset-agent-notes", dest="reset_agent_notes",
                               action="store_true",
                               help="Remove agent-added notes; keep user notes.")
    p_dirs_update.add_argument("--reset-all", dest="reset_all",
                               action="store_true",
                               help="Apply all reset flags at once.")
    p_dirs_update.add_argument("--yes", action="store_true",
                               help="Skip the confirmation prompt.")

    p_dirs_unhold = dirs_sub.add_parser("unhold", parents=[common],
                                        help="Clear a direction's hold (confirms before posting).")
    p_dirs_unhold.add_argument("direction_id", help="Direction id or prefix.")
    p_dirs_unhold.add_argument("--yes", action="store_true",
                               help="Skip the confirmation prompt.")

    p_dirs_retarget = dirs_sub.add_parser("retarget", parents=[common],
                                          help="Retarget a staged direction.")
    p_dirs_retarget.add_argument("direction_id", help="Direction id or prefix.")
    p_dirs_retarget.add_argument("version", type=int,
                                 help="New version to stage against.")
    p_dirs_retarget.add_argument("--yes", action="store_true",
                                 help="Skip the confirmation prompt.")

    p_dirs_activate = dirs_sub.add_parser("activate", parents=[common],
                                          help="Activate a staged direction immediately.")
    p_dirs_activate.add_argument("direction_id", help="Direction id or prefix.")
    p_dirs_activate.add_argument("--yes", action="store_true",
                                  help="Skip the confirmation prompt.")

    p_dirs_cancel = dirs_sub.add_parser("cancel", parents=[common],
                                        help="Cancel a staged direction.")
    p_dirs_cancel.add_argument("direction_id", help="Direction id or prefix.")
    p_dirs_cancel.add_argument("--yes", action="store_true",
                                help="Skip the confirmation prompt.")

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

    # messages
    p_msgs = sub.add_parser(
        "messages", parents=[common],
        help="Read or send operator<->system messages.",
        description="List open messages or send a new one to the autonomous system. "
                    "Defaults to `list` when no subcommand is given.",
    )
    msgs_sub = p_msgs.add_subparsers(dest="messages_cmd")
    p_msgs_list = msgs_sub.add_parser("list", parents=[common], help="List messages.")
    p_msgs_list.add_argument(
        "--status", default="open",
        help="Filter by status (default: open; pass empty string for all).",
    )
    p_msgs_send = msgs_sub.add_parser("send", parents=[common],
                                      help="Send a message to the autonomous system.")
    p_msgs_send.add_argument("content", help="Message content.")
    p_msgs_send.add_argument(
        "--type", default="FEEDBACK",
        help="Message type: FEEDBACK|NOTE|BUG_REPORT|QUESTION|BLOCKER (default FEEDBACK).",
    )
    p_msgs_answer = msgs_sub.add_parser("answer", parents=[common],
                                        help="Answer a QUESTION message with a threaded reply.")
    p_msgs_answer.add_argument("message_id", help="ID of the message to answer.")
    p_msgs_answer.add_argument("content", help="Answer content.")
    p_msgs_confirm = msgs_sub.add_parser(
        "confirm", parents=[common],
        help="List or respond to strategist pending_user_confirmation items.",
        description="List pending confirmations from the problem model, then accept or dismiss them. "
                    "Defaults to `list` when no subcommand is given.",
    )
    confirm_sub = p_msgs_confirm.add_subparsers(dest="confirm_cmd")
    confirm_sub.add_parser("list", parents=[common], help="List pending confirmations.")
    p_confirm_accept = confirm_sub.add_parser(
        "accept", parents=[common],
        help="Accept a pending confirmation (applies intent_change or records decision).")
    p_confirm_accept.add_argument("pending_id", help="ID of the pending confirmation item.")
    p_confirm_accept.add_argument("--answer", default=None,
                                  help="Operator answer text (for major_decision items).")
    p_confirm_accept.add_argument("--yes", action="store_true",
                                  help="Skip the confirmation prompt.")
    p_confirm_dismiss = confirm_sub.add_parser(
        "dismiss", parents=[common],
        help="Dismiss a pending confirmation (records dismissal in revision log).")
    p_confirm_dismiss.add_argument("pending_id", help="ID of the pending confirmation item.")
    p_confirm_dismiss.add_argument("--note", default=None,
                                   help="Optional note explaining the dismissal.")
    p_confirm_dismiss.add_argument("--yes", action="store_true",
                                   help="Skip the confirmation prompt.")

    # logs
    p_logs = sub.add_parser("logs", parents=[common], help="Read Cloud Run logs via gcloud.",
                            description="Read Cloud Run logs via gcloud. Requires "
                                        "`gcloud auth login`. Subcommands: errors (recent errors), "
                                        "tail (recent logs), search (grep by pattern), "
                                        "task (grep by task id).")
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

    # fleet
    p_fleet = sub.add_parser(
        "fleet", parents=[common],
        help="VM account pool, warm pool, and zone health.",
        description="Show Max Plan account pool, warm VM pool, and zone health. "
                    "Defaults to `pool` when no subcommand is given.")
    fleet_sub = p_fleet.add_subparsers(dest="fleet_cmd")
    fleet_sub.add_parser("pool", parents=[common], help="Show Max Plan account pool.")
    fleet_sub.add_parser("warm", parents=[common],
                         help="Warm VM pool funnel: sizing, dispatch hit rate, and cost guard.")
    p_fleet_accounts = fleet_sub.add_parser(
        "accounts", parents=[common],
        help="Show or manage Max Plan accounts (--disable/--enable/--remove).")
    p_fleet_accounts.add_argument("--disable", metavar="NAME",
                                  help="Disable account by name/prefix, or 'all'.")
    p_fleet_accounts.add_argument("--enable", metavar="NAME",
                                  help="Enable account by name/prefix, or 'all'.")
    p_fleet_accounts.add_argument("--remove", metavar="NAME",
                                  help="Remove account by name/prefix.")
    p_fleet_accounts.add_argument("--force", action="store_true",
                                  help="Force remove even with active tasks.")
    p_fleet_accounts.add_argument("--yes", action="store_true",
                                  help="Skip the confirmation prompt.")
    fleet_sub.add_parser("zones", parents=[common], help="Show zone health.")

    # health
    p_health = sub.add_parser(
        "health", parents=[common],
        help="System health: deploy state, autonomy, alerts, digest, activity.",
        description="System health subcommands. Defaults to `deploy` when no subcommand is given.")
    health_sub = p_health.add_subparsers(dest="health_cmd")
    health_sub.add_parser("deploy", parents=[common],
                          help="Deploy health deep-dive: probe ring buffer, external deps, orchestrator.")
    health_sub.add_parser("autonomy", parents=[common],
                          help="Autonomous research state per project.")
    p_health_alerts = health_sub.add_parser("alerts", parents=[common],
                                            help="Show or dismiss system alerts.")
    p_health_alerts.add_argument("--dismiss", metavar="ALERT_ID",
                                 help="Dismiss a specific alert by id (confirms via read-only guard).")
    p_health_alerts.add_argument("--dismiss-all", dest="dismiss_all", action="store_true",
                                 help="Dismiss all active alerts.")
    p_health_digest = health_sub.add_parser("digest", parents=[common],
                                            help="Catch-up digest (default last 24h).")
    p_health_digest.add_argument("--hours", type=int, default=24,
                                 help="Lookback window in hours (default 24).")
    p_health_activity = health_sub.add_parser("activity", parents=[common],
                                              help="Recent activity feed.")
    p_health_activity.add_argument("--limit", type=int, default=20,
                                   help="Max activity events to show (default 20).")

    # settings
    p_settings = sub.add_parser(
        "settings", parents=[common],
        help="Show or update runtime settings (autonomy flags, cost ceilings, pool sizing).",
        description="Read or patch runtime configuration. Defaults to `show` when no "
                    "subcommand is given.")
    settings_sub = p_settings.add_subparsers(dest="settings_cmd")
    settings_sub.add_parser("show", parents=[common], help="Show all runtime settings.")
    p_settings_set = settings_sub.add_parser(
        "set", parents=[common],
        help="Update one or more settings (KEY=VALUE ...).")
    p_settings_set.add_argument(
        "assignments", nargs="+", metavar="KEY=VALUE",
        help="Setting assignments (e.g. max_concurrent_vms=5 promotion_enabled=true).")
    p_settings_set.add_argument("--yes", action="store_true",
                                help="Skip the confirmation prompt (for scripting).")

    # envelope
    p_envelope = sub.add_parser(
        "envelope", parents=[common],
        help="Show or update the per-project resource envelope (VM cap, spend ceiling).",
        description="Read or patch the resource envelope for a project. "
                    "Defaults to `show` when no subcommand is given.")
    envelope_sub = p_envelope.add_subparsers(dest="envelope_cmd")
    envelope_sub.add_parser("show", parents=[common],
                            help="Show the current resource envelope.")
    p_env_set = envelope_sub.add_parser(
        "set", parents=[common],
        help="Patch one or more envelope fields (KEY=VALUE ...).")
    p_env_set.add_argument("assignments", nargs="+", metavar="KEY=VALUE",
                           help="Fields to update (e.g. max_concurrent_vms=5 "
                                "spend_ceiling_usd=100.0).")
    p_env_set.add_argument("--yes", action="store_true",
                           help="Skip the confirmation prompt (for scripting).")

    p_analytics = sub.add_parser(
        "analytics", parents=[common],
        help="Pipeline analytics: cost, duration, pause-stats, failing-types.",
        description="Analytics subcommands. Defaults to `cost` when no subcommand is given.")
    analytics_sub = p_analytics.add_subparsers(dest="analytics_cmd")
    p_analytics_cost = analytics_sub.add_parser("cost", parents=[common],
                                                help="Task cost breakdown by type and direction.")
    p_analytics_cost.add_argument("--window", default="1h",
                                  choices=["15m", "30m", "1h", "2h", "3h", "6h", "12h", "24h", "all"],
                                  help="Time window (default 1h).")
    analytics_sub.add_parser("duration", parents=[common],
                             help="Task duration stats (p50/p90 by type and scope).")
    p_analytics_pause = analytics_sub.add_parser("pause-stats", parents=[common],
                                                 help="Direction auto-pause/recovery stats.")
    p_analytics_pause.add_argument("--hours", type=int, default=24,
                                   help="Lookback window in hours (default 24).")
    analytics_sub.add_parser("failing-types", parents=[common],
                             help="Task types currently gated by high failure rate.")

    # vision
    p_vision = sub.add_parser("vision", parents=[common],
                              help="Show product vision and design document.")
    p_vision.add_argument(
        "--design-doc", dest="design_doc_key", nargs="?", const="",
        help="Show design document pages; optionally pass a page KEY to show one page.",
    )

    # strategic (resident session: start / stop / chat)
    p_strategic = sub.add_parser(
        "strategic", parents=[common],
        help="Resident Strategic session: start, stop, or chat from the terminal.",
        description="Start (or adopt) a resident strategist session on the operator's "
                    "runner, stop it, or chat with it interactively.")
    strategic_sub = p_strategic.add_subparsers(dest="strategic_cmd")
    p_strat_start = strategic_sub.add_parser(
        "start", parents=[common],
        help="Start (or adopt) the resident Strategic session.")
    p_strat_start.add_argument(
        "--runner-id", dest="runner_id", default=None,
        help="Pin the session to a specific local runner target id.")
    p_strat_stop = strategic_sub.add_parser(
        "stop", parents=[common],
        help="Stop the resident Strategic session and release authority.")
    p_strat_stop.add_argument("--yes", action="store_true",
                              help="Skip the confirmation prompt.")
    p_strat_chat = strategic_sub.add_parser(
        "chat", parents=[common],
        help="Interactive terminal chat with the resident strategist.")
    p_strat_chat.add_argument(
        "--interval", type=float, default=3.0,
        help="Seconds between conversation polls (default 3).")
    p_strat_chat.add_argument(
        "--timeout", type=float, default=120.0,
        help="Max seconds to wait for a reply per turn (default 120).")
    p_strat_chat.add_argument(
        "--type", dest="type", default="NOTE",
        help="Message type to send (default NOTE).")

    # strategic-cycle
    p_sc = sub.add_parser("strategic-cycle", parents=[common],
                          help="Manually trigger a strategic cycle.")
    p_sc.add_argument("--phase", default="design_doc",
                      choices=["design_doc", "strategic_review"],
                      help="Cycle phase (default: design_doc, chains to strategic_review).")
    p_sc.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    # trim-playbook-archive
    p_tpa = sub.add_parser("trim-playbook-archive", parents=[common],
                           help="Trim design_doc.playbook_archive to the last N entries.")
    p_tpa.add_argument("--keep", type=int, default=50,
                       help="Number of entries to keep (default 50).")
    p_tpa.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="Report size without writing changes.")

    # fingerprints
    p_fp = sub.add_parser("fingerprints", parents=[common],
                          help="List or manage residual remediation cases.")
    p_fp.add_argument("--all", dest="all_statuses", action="store_true",
                      help="Show cases in every status.")
    p_fp.add_argument("--status", action="append", default=[], metavar="STATUS",
                      help="Filter by status (repeatable).")
    p_fp.add_argument("--ignore", metavar="FP_SUBSTRING",
                      help="Ignore a residual case by fingerprint substring.")
    p_fp.add_argument("--unignore", metavar="FP_SUBSTRING",
                      help="Unignore a residual case by fingerprint substring.")
    p_fp.add_argument("--clear", action="store_true",
                      help="Wipe the in-memory residual store.")

    # regression-cases
    p_rc = sub.add_parser("regression-cases", parents=[common],
                          help="List or close regression remediation cases.")
    p_rc.add_argument("--all", dest="all_statuses", action="store_true",
                      help="Show cases in every status.")
    p_rc.add_argument("--status", action="append", default=[], metavar="STATUS",
                      help="Filter by status (repeatable).")
    p_rc.add_argument("--close", metavar="CLASS",
                      help="Close a regression case by class name.")
    p_rc.add_argument("--close-prefix", dest="close_prefix", metavar="PREFIX",
                      help="Close all open cases with a matching class prefix.")
    p_rc.add_argument("--reason", help="Reason for closing (recorded on the case).")

    # knowledge
    p_knowledge = sub.add_parser(
        "knowledge", parents=[common],
        help="Browse the knowledge library (web_research / design documents).",
        description="List and show knowledge documents accumulated across research cycles. "
                    "Defaults to `list` when no subcommand is given.")
    knowledge_sub = p_knowledge.add_subparsers(dest="knowledge_cmd")
    p_kl = knowledge_sub.add_parser("list", parents=[common],
                                    help="List knowledge documents with optional filters.")
    p_kl.add_argument("--direction", help="Filter by direction id.")
    p_kl.add_argument("--source-kind", dest="source_kind",
                      help="Filter by source kind (e.g. web_research, design).")
    p_kl.add_argument("--q", help="Full-text search query.")
    p_kl.add_argument("--topic", help="Filter by topic.")
    p_kl.add_argument("--tag", help="Filter by tag (exact match).")
    p_kl.add_argument("--limit", type=int, default=20,
                      help="Max documents to return (default 20).")
    p_ks = knowledge_sub.add_parser("show", parents=[common],
                                    help="Show a knowledge document with its full body.")
    p_ks.add_argument("doc_id", help="Knowledge document id.")
    p_ksw = knowledge_sub.add_parser(
        "sweep", parents=[common],
        help="Show a side-by-side arm comparison table for a compute-experiment sweep.",
    )
    p_ksw.add_argument("sweep_id", help="Sweep id (e.g. swp-abc123).")

    # compute
    p_compute = sub.add_parser(
        "compute", parents=[common],
        help="Launch, list, kill, and analyze compute experiments.",
        description="Manage GPU/accelerator compute jobs. Defaults to `list` when no "
                    "subcommand is given.")
    compute_sub = p_compute.add_subparsers(dest="compute_cmd")
    p_compute_list = compute_sub.add_parser("list", parents=[common],
                                            help="List active and recent compute jobs.")
    p_compute_list.add_argument(
        "--include-finished", dest="include_finished", type=int, default=360,
        help="Include jobs that finished within this many minutes (default 360).")
    p_compute_list.add_argument(
        "--all-terminal", dest="all_terminal", action="store_true",
        help="List all terminal jobs regardless of age (bypasses the 7-day window limit).")
    p_compute_list.add_argument("--limit", type=int, default=20,
                                help="Max jobs to return (default 20).")
    p_compute_list.add_argument("--status", dest="status_filter", default=None,
                                help="Filter rows by status (e.g. RUNNING, SUCCEEDED, FAILED).")
    p_compute_show = compute_sub.add_parser(
        "show", parents=[common],
        help="Show full details for one compute job (metrics, verdict, knowledge doc). "
             "Searches all history regardless of age.")
    p_compute_show.add_argument("job_id", help="Compute job id prefix or full UUID.")
    p_compute_kill = compute_sub.add_parser(
        "kill", parents=[common],
        help="Kill a running compute job (confirms before mutating).")
    p_compute_kill.add_argument("job_id", help="Compute job id (UUID).")
    p_compute_kill.add_argument("--yes", action="store_true",
                                help="Skip the confirmation prompt.")
    p_compute_analyze = compute_sub.add_parser(
        "analyze", parents=[common],
        help="Trigger LLM analysis for a terminal compute job (confirms before posting).")
    p_compute_analyze.add_argument("job_id", help="Compute job id (UUID).")
    p_compute_analyze.add_argument("--yes", action="store_true",
                                   help="Skip the confirmation prompt.")
    p_compute_run = compute_sub.add_parser(
        "run", parents=[common],
        help="Run a compute_experiment command locally (no orchestrator, no DB, no GCP).",
        description="Execute a compute_experiment command locally on this machine. "
                    "Sets BLUEPRINT_RESULT_FILE and BLUEPRINT_ARTIFACT_DIR so the command "
                    "can emit metrics via the standard file contract, then reports the result.")
    p_compute_run.add_argument(
        "run_command", nargs="?", default=None,
        help="Bash command string to execute. "
             "Write a flat JSON object of {metric: value} to $BLUEPRINT_RESULT_FILE to report metrics.")
    p_compute_run.add_argument(
        "--local", dest="local_spec", metavar="SPEC_JSON",
        help="Path to a spec JSON file. The JSON must contain 'command' (str), and "
             "optionally 'declared_metric' (str) and 'success_criterion' (object with "
             "metric/operator/threshold). Runs the command locally without the orchestrator."
    )
    p_compute_run.add_argument(
        "--image", dest="container_image", default=None, metavar="IMAGE_URI",
        help="Container image URI to run the command inside "
             "(e.g. nvcr.io/nvidia/tensorflow:25.01-tf2-py3). "
             "When set the command is wrapped in docker run with the result-file "
             "directory volume-mounted. --gpus all is added when a local GPU is detected. "
             "Spec file takes precedence over this flag.")
    p_compute_run.add_argument(
        "--criterion-metric", dest="criterion_metric", default=None, metavar="METRIC",
        help="Metric name for success criterion (positional form only; spec takes precedence).")
    p_compute_run.add_argument(
        "--criterion-op", dest="criterion_op", default=None, metavar="OP",
        help="Operator for success criterion: >= <= > < == !=")
    p_compute_run.add_argument(
        "--criterion-threshold", dest="criterion_threshold", type=float, default=None,
        metavar="THRESHOLD",
        help="Threshold for success criterion.")
    p_compute_launch = compute_sub.add_parser(
        "launch", parents=[common],
        help="Launch a cloud compute experiment on the orchestrator (confirms before mutating).",
        description="POST a compute_experiment task to the orchestrator. "
                    "Use --dud for a no-repo substrate probe, or supply --command for a custom experiment.")
    p_compute_launch.add_argument(
        "--repo", default=None, metavar="OWNER/REPO",
        help="GitHub repo for the experiment (default: default_repo from config).")
    p_compute_launch.add_argument(
        "--dud", action="store_true",
        help="Use the built-in no-repo substrate-probe command with dud_ok>=1 criterion.")
    p_compute_launch.add_argument(
        "--command", dest="experiment_command", default=None,
        help="Bash command string for the experiment (ignored when --dud).")
    p_compute_launch.add_argument(
        "--title", default=None, help="Task title (default: 'operator substrate probe' for --dud).")
    p_compute_launch.add_argument(
        "--prompt", default=None, help="Task prompt / description.")
    p_compute_launch.add_argument(
        "--base-branch", dest="base_branch", default=None,
        help="Base branch (default: default_base_branch from config or 'main').")
    p_compute_launch.add_argument(
        "--spend-cap", dest="spend_cap", type=float, default=0.50,
        help="Spend cap in USD (default 0.50).")
    p_compute_launch.add_argument(
        "--deadline", type=int, default=600,
        help="Deadline in seconds (default 600).")
    p_compute_launch.add_argument(
        "--metric", default=None,
        help="Success-criterion metric name (default 'dud_ok' for --dud).")
    p_compute_launch.add_argument(
        "--operator", default=">=",
        help="Success-criterion operator: >= <= > < == != (default >=).")
    p_compute_launch.add_argument(
        "--threshold", type=float, default=None,
        help="Success-criterion threshold (default 1.0 for --dud).")
    p_compute_launch.add_argument(
        "--machine-type", dest="machine_type", default=None,
        help="GCP machine type override (e.g. n1-standard-4).")
    p_compute_launch.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt.")

    # playbook
    p_pb = sub.add_parser("playbook", parents=[common],
                          help="List, delete, or prune playbook entries.",
                          description="Inspect or manage project playbook entries. "
                                      "Defaults to listing all entries.")
    pb_sub = p_pb.add_subparsers(dest="playbook_cmd")
    pb_sub.add_parser("list", parents=[common], help="List all playbook entries.")
    p_pb_delete = pb_sub.add_parser("delete", parents=[common],
                                    help="Delete a playbook entry by key.")
    p_pb_delete.add_argument("key", help="Playbook entry key.")
    p_pb_prune = pb_sub.add_parser("prune", parents=[common],
                                   help="Delete entries matching a pattern.")
    p_pb_prune.add_argument("pattern", help="Case-insensitive regex pattern.")
    p_pb_prune.add_argument("--yes", action="store_true",
                            help="Skip the confirmation prompt.")

    return parser


_DISPATCH = {
    "projects": cmd_projects,
    "status": cmd_status,
    "diagnostics": cmd_diagnostics,
    "ops": cmd_ops,
    "tasks": cmd_tasks,
    "versions": cmd_versions,
    "system": cmd_system,
    "monitor": cmd_monitor,
    "directions": cmd_directions,
    "trials": cmd_trials,
    "fleet": cmd_fleet,
    "promote-recover": cmd_promote_recover,
    "logs": cmd_logs,
    "messages": cmd_messages,
    "settings": cmd_settings,
    "envelope": cmd_envelope,
    "health": cmd_health,
    "analytics": cmd_analytics,
    "vision": cmd_vision,
    "strategic": cmd_strategic,
    "strategic-cycle": cmd_strategic_cycle,
    "trim-playbook-archive": cmd_trim_playbook_archive,
    "fingerprints": cmd_fingerprints,
    "regression-cases": cmd_regression_cases,
    "playbook": cmd_playbook,
    "knowledge": cmd_knowledge,
    "compute": cmd_compute,
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
    try:
        # Resolve --project <name> to an id once, up front, so every
        # get_project_id(args.project_id) call site targets it. An explicit
        # --project-id wins if both are supplied.
        proj_name = getattr(args, "project_name", None)
        if proj_name and not getattr(args, "project_id", None):
            args.project_id = resolve_project_id_by_name(proj_name)
        handler(args)
    except APIError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
