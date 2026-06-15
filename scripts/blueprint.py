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
    promote-recover   promotion + recovery operator levers (confirm before mutate)
    logs tail|errors|search|task   Cloud Logging reads (via gcloud)

Examples:
    python scripts/blueprint.py status
    python scripts/blueprint.py --json status
    python scripts/blueprint.py diagnostics
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

from blueprint_api import APIError, api, get_project_id, load_config

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
    fleet_raw = state.get("fleet") or {}

    serving = (version.get("serving") or {}) if version else {}

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
    """Dispatch task subcommands: list / show / cancel / requeue / scope / tree / failures."""
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
    result = api("PATCH", f"/api/admin/tasks/{args.task_id}", {"status": "QUEUED"})
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
                summary["tasks_merged"] = (ver_summary or {}).get("tasks_merged") or []
                summary["highlights"] = (ver_summary or {}).get("highlights")
                summary["serving_status"] = (ver_summary or {}).get("serving_status")
            else:
                summary["tasks_merged"] = []
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
    tasks_merged = v.get("tasks_merged") or []
    tasks_str = f"  tasks={len(tasks_merged)}" if tasks_merged else ""
    return f"  {marker} v{num}  [{status:9}] phase={phase:9} sha={sha}{merged_str}{tasks_str}"


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
    if headline:
        print(f"\n  Summary: {headline}")
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
        description="Unified Blueprint operator CLI (status/diagnostics/tasks/"
                    "versions/promote-recover/logs).",
        epilog="Auth: set BLUEPRINT_API_KEY + BLUEPRINT_API_URL env vars, or place "
               "~/.blueprint-config.json (keys: api_url, api_key), or run "
               "`gcloud auth login` to auto-bootstrap.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", parents=[common],
                   help="Version phase, deploy state, and promotion eligibility.")

    # diagnostics
    sub.add_parser("diagnostics", parents=[common],
                   help="Stuck tasks, circuit breakers, and grouped failure modes.")

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

    # versions
    p_versions = sub.add_parser("versions", parents=[common],
                                help="List release versions or show a summary.",
                                description="List release versions or show a detailed summary "
                                            "for a specific version.")
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

    return parser


_DISPATCH = {
    "status": cmd_status,
    "diagnostics": cmd_diagnostics,
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
    try:
        handler(args)
    except APIError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
