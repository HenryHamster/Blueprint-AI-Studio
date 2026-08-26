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
    projects list|onboard  project inventory; onboard a repo as a new project
    status            current version phase, deploy state, promotion eligibility
    diagnostics       stuck tasks, circuit breakers, grouped failure modes
    tasks list|show   list/filter tasks, or show one by id
    versions list|show  release versions with phase/status/sha and a summary
    trials            capability-trial verdict, arm counts, days-to-decision projection
    fleet pool|warm|accounts|zones|runners|grant|revoke
                      VM account pool, warm pool, zone health, and the local
                      runner <-> project allow-list bindings
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
    python scripts/blueprint.py fleet runners
    python scripts/blueprint.py fleet grant --runner iconlab --project-id <uuid>
    python scripts/blueprint.py projects onboard --owner acme --repo widget \
        --goal-file goal.md
    python scripts/blueprint.py promote-recover qualify --yes
    python scripts/blueprint.py logs errors --minutes 30

See also:
    scripts/blueprint_runner.py — the separate CLI for local runner management
    (start, check, install, uninstall, update, status).  All runner lifecycle
    operations are in blueprint_runner.py; blueprint.py has no runner
    subcommands.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import ntpath
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import urlopen

try:
    import fcntl
except ImportError:  # Windows: read-only/operator commands remain supported.
    fcntl = None  # type: ignore[assignment]

from blueprint_api import (
    APIError,
    api,
    get_project_id,
    list_projects,
    load_config,
    resolve_project_id_by_name,
    resolve_task_id_by_prefix,
    task_id_needs_prefix_resolution,
    validate_project_id,
)

# Keep the retry policy shared with the backend's activation contract.  The
# bootstrap copies this standalone module beside blueprint.py; source-tree CLI
# runs load the same file from backend/app.
_ACTIVATION_MODULE = Path(__file__).resolve().with_name("release_activation.py")
if not _ACTIVATION_MODULE.is_file():
    _ACTIVATION_MODULE = (
        Path(__file__).resolve().parents[1] / "backend" / "app" / "release_activation.py"
    )
if not _ACTIVATION_MODULE.is_file():
    raise ImportError(f"activation retry module missing: {_ACTIVATION_MODULE}")

_activation_spec = importlib.util.spec_from_file_location(
    "_blueprint_release_activation", _ACTIVATION_MODULE
)
if _activation_spec is None or _activation_spec.loader is None:
    raise ImportError(f"cannot load activation retry module: {_ACTIVATION_MODULE}")
_activation_module = importlib.util.module_from_spec(_activation_spec)
_activation_spec.loader.exec_module(_activation_module)
ActivationPhaseFailure = _activation_module.ActivationPhaseFailure
retry_activation_phase = _activation_module.retry_activation_phase

SERVICE_NAME = os.environ.get("BLUEPRINT_GCP_SERVICE", "blueprint-orchestrator")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "blueprint-orchestrator")

_DEPLOY_AGENT_UNIT_PATH = "/etc/systemd/system/blueprint-deploy-agent.service"
_DEPLOY_AGENT_POLL_PATH = "/run/blueprint-deploy-agent/deploy-agent.poll"
_DEPLOY_AGENT_STATE_PATH = "/var/lib/blueprint-deploy-agent/deploy-agent.state"
_RELEASE_SOCKET_PATH = "/run/blueprint-release.sock"
_RELEASE_SOCKET_TIMEOUT_SECONDS = 60
_RELEASE_SHIM_PATH = "/usr/local/sbin/blueprint-release"
_DEPLOY_AGENT_POLL_MAX_AGE_SECONDS = 120
# The root release shim can take several minutes to pull and verify an image.
# Once it has accepted the handoff, give the locally served API a bounded window
# to prove the promoted revision before treating the activation as failed.
_DEPLOY_AGENT_CONFIRMATION_TIMEOUT_SECONDS = 15 * 60
_LOCAL_SERVING_VERSION_URL = "http://127.0.0.1:8080/api/version"
_ACTIVATION_REARMABLE_SERVING_STATUSES = frozenset(
    {"unconfirmed", "confirmation_timed_out"}
)


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

# Mirrors ``OnboardPayload.goal``'s ``max_length`` in backend/app/schemas.py.
# Validated client-side so a long --goal-file fails with the actual length
# instead of an opaque server 422.
_ONBOARD_GOAL_MAX_CHARS = 5000

# build_config keys worth echoing after an onboard. ``has_ci`` and
# ``requires_ci_checks`` are printed unconditionally by _render_onboard (even
# when detection omitted them) because their combination is a known wedge.
_ONBOARD_NOTABLE_BUILD_FLAGS = (
    "language",
    "package_manager",
    "test_command",
    "build_command",
    "lint_command",
    "task_branch_namespace",
    "repo_is_empty",
    "detection_incomplete",
)


def cmd_projects(args: argparse.Namespace) -> None:
    """Dispatch project subcommands: ``list`` (default) / ``onboard``."""
    sub = getattr(args, "projects_cmd", None) or "list"
    if sub == "onboard":
        _projects_onboard(args)
        return
    _projects_list(args)


def _projects_list(args: argparse.Namespace) -> None:
    """List every project with its canonical operational state. Respects ``--json``."""
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
        status = (
            p.get("operational_state")
            or p.get("state")
            or p.get("status")
            or "-"
        )
        print(f"  {pid} · {name} · {status}")


def _onboard_goal(args: argparse.Namespace) -> str | None:
    """Resolve ``--goal`` / ``--goal-file`` into the payload's goal string.

    ``--goal-file`` exists so a multi-paragraph goal never has to be shell-quoted
    into the command line. The length ceiling is enforced here rather than left
    to the server: a 422 names neither the offending length nor which flag
    carried it, and the file path is exactly where an over-long goal comes from.
    """
    goal = getattr(args, "goal", None)
    goal_file = getattr(args, "goal_file", None)
    if goal_file:
        path = Path(goal_file).expanduser()
        try:
            goal = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot read --goal-file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
        except UnicodeDecodeError as exc:
            print(f"ERROR: --goal-file {path} is not valid UTF-8: {exc}",
                  file=sys.stderr)
            sys.exit(1)
    if goal is None:
        return None
    goal = goal.strip()
    if not goal:
        return None
    if len(goal) > _ONBOARD_GOAL_MAX_CHARS:
        source = f"--goal-file {goal_file}" if goal_file else "--goal"
        print(
            f"ERROR: {source} is {len(goal)} characters; the onboarding goal is "
            f"capped at {_ONBOARD_GOAL_MAX_CHARS} (OnboardPayload.goal max_length). "
            f"Trim it by {len(goal) - _ONBOARD_GOAL_MAX_CHARS} characters and retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    return goal


def _projects_onboard(args: argparse.Namespace) -> None:
    """Create a project from a GitHub repo via POST /api/admin/projects/onboard.

    ``--preview`` maps to ``preview_only=True``, which the backend answers before
    it touches the database — so it needs no confirmation. A real onboard creates
    a Project row, so it confirms first unless ``--yes``.
    """
    owner = args.owner
    repo = args.repo
    preview = bool(getattr(args, "preview", False))
    goal = _onboard_goal(args)

    payload: dict = {
        "github_owner": owner,
        "github_repo": repo,
        "preview_only": preview,
    }
    if getattr(args, "name", None):
        payload["name"] = args.name
    if goal:
        payload["goal"] = goal
    if getattr(args, "promotion_target", None):
        payload["promotion_target"] = args.promotion_target

    if not preview:
        _require_write_config("projects onboard")
        if not _confirm(
            f"Create a new Blueprint project for {owner}/{repo}?",
            getattr(args, "yes", False),
        ):
            print("Aborted.")
            return

    result = api("POST", "/api/admin/projects/onboard", payload)
    _emit(args, result, lambda d: _render_onboard(d, owner, repo, preview))


def _render_onboard(data: dict, owner: str, repo: str, preview: bool) -> None:
    data = data or {}
    build_config = data.get("build_config") or {}
    project_id = data.get("project_id")
    has_ci = build_config.get("has_ci")
    requires_ci = build_config.get("requires_ci_checks")

    print(f"=== ONBOARD {owner}/{repo} ===")
    print(f"  project_id:      {project_id or '(preview only — nothing created)'}")
    print(f"  default_branch:  {data.get('default_branch') or '?'}")
    print(f"  merge_verdict:   {data.get('merge_verification_verdict')}")
    print("  build_config:")
    print(f"    has_ci:               {has_ci}")
    print(f"    requires_ci_checks:   {requires_ci}")
    for key in _ONBOARD_NOTABLE_BUILD_FLAGS:
        if key in build_config:
            print(f"    {(key + ':'):22}{build_config[key]}")

    if requires_ci and not has_ci:
        print(
            "\n  WARNING: requires_ci_checks=true but has_ci=false. Every merge "
            "will block\n           forever on a check that never reports. Land CI "
            "in the repo, or\n           clear requires_ci_checks on the project's "
            "build_config, before\n           enabling autonomy."
        )

    if preview:
        print("\n  (preview only — no project was created)")
        return

    # Onboarding deliberately leaves autonomous_enabled=False (operator handoff);
    # say so, and name the exact verb that starts it.
    print("\n  Autonomy is OFF by design — onboarding hands the project to you.")
    print(
        f"  Start it with: blueprint --project-id {project_id or '<id>'} "
        "system autonomy --enable"
    )


# ── coding-agent access ──────────────────────────────────────────────────────

def cmd_agent_access(args: argparse.Namespace) -> None:
    """Manage or verify scoped coding-agent and project-operator access."""
    subcommand = getattr(args, "agent_access_cmd", None) or "whoami"
    if subcommand == "whoami":
        result = api("GET", "/api/agent-access/v1/me")
        _emit(args, result, _render_agent_access_profile)
        return
    if subcommand == "snapshot":
        project_id = get_project_id(getattr(args, "project_id", None))
        result = api(
            "GET", f"/api/agent-access/v1/projects/{project_id}/snapshot"
        )
        _emit(args, result, _render_agent_snapshot)
        return
    if subcommand == "list":
        result = api("GET", "/api/admin/service-credentials")
        _emit(args, result, _render_agent_credentials)
        return
    if subcommand == "grant":
        _require_write_config("agent-access grant")
        project_id = get_project_id(getattr(args, "project_id", None))
        name = args.name
        expires_in_days = args.expires_in_days
        profile = getattr(args, "profile", "coding-agent-read")
        project_operator = profile == "project-operator"
        scopes = ["project-operator"] if project_operator else ["coding-agent:read"]
        membership_role = "member" if project_operator else "viewer"
        prompt = (
            f"Grant {profile} access named {name!r} to project "
            f"{project_id} for {expires_in_days} day(s)?"
        )
        if not _confirm(prompt, getattr(args, "yes", False)):
            print("Aborted.")
            return
        result = api(
            "POST",
            "/api/admin/service-credentials",
            {
                "name": name,
                "description": getattr(args, "description", None),
                "scopes": scopes,
                "expires_in_days": expires_in_days,
                "project_memberships": [
                    {"project_id": project_id, "role": membership_role}
                ],
            },
        )
        _emit(args, result, _render_agent_credential_grant)
        return
    if subcommand == "revoke":
        _require_write_config("agent-access revoke")
        credential_id = args.credential_id
        if not _confirm(
            f"Revoke scoped service credential {credential_id}?",
            getattr(args, "yes", False),
        ):
            print("Aborted.")
            return
        result = api(
            "POST", f"/api/admin/service-credentials/{credential_id}/revoke", {}
        )
        _emit(args, result, lambda row: print(
            f"Revoked {row.get('name', '?')} ({row.get('id', credential_id)})."
        ))


def _render_agent_access_profile(data: dict) -> None:
    credential = data.get("credential") or {}
    print("=== SCOPED SERVICE ACCESS ===")
    print(f"  Credential: {credential.get('name', '?')}")
    print(f"  Expires:    {credential.get('expires_at') or 'never'}")
    print(f"  Scopes:     {', '.join(credential.get('scopes') or []) or '(none)'}")
    print(f"  Revision:   {(data.get('server') or {}).get('revision', '?')}")
    print("  Projects:")
    for project in data.get("projects") or []:
        repo = "/".join(filter(None, [project.get("github_owner"), project.get("github_repo")]))
        print(
            f"    {project.get('id', '?')} · {project.get('name', '?')}"
            f" · {project.get('role', '?')} · {repo or '-'}"
        )
    print("  Capabilities:")
    for capability in data.get("capabilities") or []:
        print(f"    {capability}")


def _render_agent_snapshot(data: dict) -> None:
    project = data.get("project") or {}
    attention = data.get("attention") or {}
    diagnostics = data.get("diagnostics") or {}
    resources = data.get("resources") or {}
    runners = resources.get("runners") or {}
    print(f"=== AGENT SNAPSHOT · {project.get('name', project.get('id', '?'))} ===")
    print(f"  Generated:  {data.get('generated_at', '?')}")
    print(f"  Attention:  {attention.get('level', '?')} · {attention.get('summary', '')}")
    print(
        f"  Pipeline:   {diagnostics.get('queued_count', '?')} queued · "
        f"{diagnostics.get('in_progress_count', '?')} running · "
        f"{len(diagnostics.get('stuck_tasks') or [])} stuck"
    )
    print(
        f"  Runners:    {runners.get('online', '?')}/{runners.get('total', '?')} online"
    )
    items = attention.get("items") or []
    if items:
        print("  Needs attention:")
        for item in items:
            print(
                f"    {item.get('id', '?')} · {item.get('reason', item.get('kind', '?'))}"
                f" · {item.get('label', item.get('detail', ''))}"
            )


def _render_agent_credentials(data: dict) -> None:
    credentials = data.get("service_credentials") or []
    print(f"=== SERVICE CREDENTIALS ({len(credentials)}) ===")
    for credential in credentials:
        state = "revoked" if credential.get("revoked_at") else "active"
        print(
            f"  {credential.get('id', '?')} · {credential.get('name', '?')}"
            f" · {state} · {', '.join(credential.get('scopes') or [])}"
            f" · expires {credential.get('expires_at') or 'never'}"
        )


def _render_agent_credential_grant(data: dict) -> None:
    print("=== SCOPED SERVICE CREDENTIAL CREATED ===")
    print(f"  Name:    {data.get('name', '?')}")
    print(f"  ID:      {data.get('id', '?')}")
    print(f"  Expires: {data.get('expires_at') or 'never'}")
    print(f"  Token:   {data.get('token', '?')}")
    print("  The token is shown once. Store it outside the repository.")
    if "project-operator" in set(data.get("scopes") or []):
        print("  Put it in a dedicated operator config outside the repository and")
        print("  select it explicitly with BLUEPRINT_CONFIG for operator CLI calls.")
        print("  See docs/HERMES_OPERATOR_ACCESS.md; do not put the token in prompts,")
        print("  task-worker environments, or the repository.")
    else:
        print("  Put it in a read-only config outside the repository, then point the")
        print("  MCP child at that file with BLUEPRINT_AGENT_CONFIG. See")
        print("  docs/CODING_AGENT_ACCESS.md; the model never needs the token text.")


# ── status ───────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    """Fetch canonical system state from GET /api/admin/state and version-lifecycle."""
    pid = get_project_id(getattr(args, "project_id", None))
    if _using_scoped_service_credential():
        snapshot = api("GET", f"/api/agent-access/v1/projects/{pid}/snapshot")
        _emit(args, snapshot, _render_agent_snapshot)
        return
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
    if _using_scoped_service_credential():
        pid = get_project_id(getattr(args, "project_id", None))
        snapshot = api("GET", f"/api/agent-access/v1/projects/{pid}/snapshot")
        _emit(args, snapshot, _render_agent_snapshot)
        return
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
    elif sub == "request-changes":
        _tasks_request_changes(args)
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
_ACTIVE_TASK_STATUSES = (
    "QUEUED",
    "PROVISIONING",
    "IN_PROGRESS",
    "EVALUATING",
)


def _tasks_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    scoped_service_credential = _using_scoped_service_credential()
    failure_classes = set(getattr(args, "failure_class", None) or [])
    # When client-side failure-class filtering is active, fetch a wider page so
    # the post-fetch narrowing has enough rows to work with (mirrors the web
    # console, which also filters failure_class client-side).
    display_limit = getattr(args, "limit", 50)
    fetch_limit = max(display_limit, 500) if failure_classes else display_limit
    if scoped_service_credential:
        fetch_limit = min(fetch_limit, 200)
    task_list_route = (
        "/api/agent-access/v1/tasks"
        if scoped_service_credential
        else "/api/admin/tasks"
    )
    path = f"{task_list_route}?project_id={pid}&limit={fetch_limit}"
    # --status and --blocked both contribute repeatable status= params; the
    # backend accepts a List[TaskStatus], so BLOCKED appends rather than replaces.
    raw_statuses = getattr(args, "status", None) or []
    if isinstance(raw_statuses, str):
        raw_statuses = [raw_statuses]
    requested_statuses = [str(value).upper() for value in raw_statuses]
    if getattr(args, "active", False):
        requested_statuses.extend(_ACTIVE_TASK_STATUSES)
    if getattr(args, "blocked", False):
        requested_statuses.append("BLOCKED")
    requested_statuses = list(dict.fromkeys(requested_statuses))
    for requested_status in requested_statuses:
        path += f"&status={requested_status}"
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
    payload = (
        dict(resp)
        if scoped_service_credential and isinstance(resp, dict)
        else {"tasks": tasks, "total": total}
    )
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
    scoped = _using_scoped_service_credential()
    impact: dict = {}
    cascade_count = 0
    if scoped:
        impact = api(
            "GET", f"/api/operator-access/v1/tasks/{args.task_id}/cancel-impact"
        )
        cascade_count = int(impact.get("active_child_cascade_count") or 0) + int(
            impact.get("waiting_dependent_count") or 0
        )
        if cascade_count and not getattr(args, "confirm_cascade", False):
            print(
                f"ERROR: cancelling {args.task_id} would also cancel/block "
                f"{cascade_count} child/dependent task(s). Re-run with "
                "--confirm-cascade after reviewing the impact.",
                file=sys.stderr,
            )
            raise APIError("cancel cascade confirmation required")
    prompt = f"Cancel task {args.task_id}?"
    if cascade_count:
        prompt = f"Cancel task {args.task_id} and {cascade_count} affected task(s)?"
    if not _confirm(prompt, getattr(args, "yes", False)):
        print("Aborted.")
        return
    if scoped:
        body = {
            "confirm_cascade": bool(cascade_count),
            **(
                {"reason": args.reason}
                if getattr(args, "reason", None)
                else {}
            ),
        }
        result = api(
            "POST", f"/api/operator-access/v1/tasks/{args.task_id}/cancel", body
        )
    else:
        result = api(
            "PATCH", f"/api/admin/tasks/{args.task_id}", {"status": "CANCELLED"}
        )
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
    scoped = _using_scoped_service_credential()
    guidance = getattr(args, "requeue_prompt_suffix", None)
    if scoped and (not isinstance(guidance, str) or not guidance.strip()):
        print(
            "ERROR: scoped project-operator requeue requires non-empty --guidance.",
            file=sys.stderr,
        )
        raise APIError("requeue guidance is required")
    if not _confirm(f"Requeue task {args.task_id}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    body = {"status": "QUEUED"}
    if guidance is not None:
        guidance = guidance.strip()
        if not guidance:
            print("ERROR: --prompt-suffix/--guidance cannot be empty", file=sys.stderr)
            sys.exit(2)
        body["requeue_prompt_suffix"] = guidance
    if scoped:
        result = api(
            "POST",
            f"/api/operator-access/v1/tasks/{args.task_id}/requeue",
            {"guidance": guidance},
        )
    else:
        result = api("PATCH", f"/api/admin/tasks/{args.task_id}", body)
    _emit(args, result, lambda d: print(f"  {args.task_id}: {d.get('status', 'ok')}"))


def _tasks_request_changes(args: argparse.Namespace) -> None:
    _require_write_config("tasks request-changes")
    if not _confirm(
        f"Request {args.action} follow-up for task {args.task_id}?",
        getattr(args, "yes", False),
    ):
        print("Aborted.")
        return
    route = (
        f"/api/operator-access/v1/tasks/{args.task_id}/request-changes"
        if _using_scoped_service_credential()
        else f"/api/admin/tasks/{args.task_id}/request-changes"
    )
    result = api(
        "POST", route, {"feedback": args.feedback, "action": args.action}
    )
    _emit(
        args,
        result,
        lambda row: print(
            f"  spawned {row.get('action', args.action)} task: "
            f"{row.get('spawned_task_id', '?')}"
        ),
    )


def _using_scoped_service_credential() -> bool:
    """Return whether the selected config carries a scoped service token."""
    return str(load_config().get("api_key") or "").startswith("bpsc_")


_SCOPED_PROFILE_CACHE: tuple[str, dict] | None = None
_SCOPED_READ_COMMANDS = frozenset(
    {
        ("projects", None),
        ("agent-access", "whoami"),
        ("agent-access", "snapshot"),
        ("status", None),
        ("diagnostics", None),
        ("tasks", "list"),
        ("tasks", "show"),
        ("directions", "list"),
        ("directions", "show"),
        ("messages", "list"),
    }
)
_PROJECT_OPERATOR_WRITE_COMMANDS = frozenset(
    {
        ("tasks", "create"),
        ("tasks", "cancel"),
        ("tasks", "requeue"),
        ("tasks", "request-changes"),
        ("directions", "add"),
        ("directions", "update"),
        ("directions", "retarget"),
        ("directions", "activate"),
        ("directions", "cancel"),
        ("messages", "send"),
        ("messages", "answer"),
        ("strategic", "start"),
        ("strategic", "chat"),
        ("strategic", "send"),
        ("strategic-cycle", None),
    }
)


# Verbs that are, by contract, "no orchestrator, no DB, no GCP". They must not
# be routed through credential resolution at all: `compute run` used to reach
# load_config() via _enforce_scoped_cli_command -> _using_scoped_service_credential
# and die telling the operator to `gcloud auth login` before a purely local
# experiment could start. Declared structurally, as an exact (command,
# subcommand) set, so exempting a verb is a visible one-line change here and not
# a string match scattered across call sites. Adding a verb that DOES touch the
# API to this set would bypass the ServiceCredential fence — never do that.
_LOCAL_ONLY_CLI_COMMANDS = frozenset({("compute", "run")})


def _scoped_command_key(args: argparse.Namespace) -> tuple[str, str | None]:
    command = str(getattr(args, "command", ""))
    subcommand_attr = {
        "agent-access": "agent_access_cmd",
        "projects": "projects_cmd",
        "tasks": "tasks_cmd",
        "directions": "directions_cmd",
        "messages": "messages_cmd",
        "strategic": "strategic_cmd",
        "compute": "compute_cmd",
    }.get(command)
    subcommand = getattr(args, subcommand_attr, None) if subcommand_attr else None
    # Bare `projects` and `projects list` are the same read, so both map to the
    # single ("projects", None) allow-list entry. `projects onboard` therefore
    # stays outside both allow-lists and is denied for scoped credentials — it
    # creates a project, which is a platform action, not a project-operator one.
    if command == "projects" and subcommand == "list":
        subcommand = None
    if subcommand is None and command in {"tasks", "directions", "messages"}:
        subcommand = "list"
    if subcommand is None and command == "agent-access":
        subcommand = "whoami"
    return command, subcommand


def _scoped_service_profile() -> dict:
    global _SCOPED_PROFILE_CACHE
    token = str(load_config().get("api_key") or "")
    if _SCOPED_PROFILE_CACHE is None or _SCOPED_PROFILE_CACHE[0] != token:
        profile = api("GET", "/api/agent-access/v1/me")
        if not isinstance(profile, dict):
            raise APIError("invalid scoped ServiceCredential profile")
        _SCOPED_PROFILE_CACHE = (token, profile)
    return _SCOPED_PROFILE_CACHE[1]


def _is_local_only_cli_command(args: argparse.Namespace) -> bool:
    """True for verbs that never touch the orchestrator, DB, or GCP."""
    return _scoped_command_key(args) in _LOCAL_ONLY_CLI_COMMANDS


def _enforce_scoped_cli_command(args: argparse.Namespace) -> None:
    """Deny unsupported ServiceCredential commands before any local side effect.

    The credential-free contract for local-only verbs (#17889) is "never DIE on
    a missing config", NOT "never READ the config". Those are different, and
    conflating them opened a hole: `compute` is a host command that
    CLAUDE.md requires to fail closed for the project-operator profile, but the
    local-only exemption used to be taken BEFORE the scoped check, so a scoped
    ServiceCredential ran `compute run` unfenced. You cannot decide whether a
    scoped credential is selected without looking at the config, so the probe
    happens here and only a genuinely unresolvable config is waved through.
    """
    try:
        scoped = _using_scoped_service_credential()
    except (APIError, OSError, ValueError):
        # No resolvable Blueprint config: absent (APIError), unreadable
        # (OSError), or malformed (ValueError, which json.JSONDecodeError
        # subclasses). A local-only verb is credential-free by contract and
        # must still run; every other verb reaches its own load_config()
        # moments later and fails there with the actionable setup hint.
        # Waving THIS case through cannot grant a scoped credential anything,
        # because a config that will not resolve holds no usable credential.
        return
    if not scoped:
        return
    # A scoped ServiceCredential IS selected. Local-only verbs get no exemption
    # from here down: the allowlists below are the whole authority, and
    # ("compute", "run") is deliberately absent from both.
    key = _scoped_command_key(args)
    profile = _scoped_service_profile()
    scopes = set((profile.get("credential") or {}).get("scopes") or [])
    allowed = key in _SCOPED_READ_COMMANDS and bool(
        scopes.intersection({"coding-agent:read", "project-operator"})
    )
    allowed = allowed or (
        key in _PROJECT_OPERATOR_WRITE_COMMANDS and "project-operator" in scopes
    )
    if not allowed:
        label = " ".join(part for part in key if part)
        print(
            f"ERROR: scoped ServiceCredential command not permitted: {label}. "
            "The project-operator profile exposes only exact project task, "
            "direction, message, and Strategic actions; platform admin, bulk, "
            "deploy, migrate, provider/fleet/runner, and host commands are denied.",
            file=sys.stderr,
        )
        raise APIError("scoped ServiceCredential command not permitted")


def _tasks_show(args: argparse.Namespace) -> None:
    if _using_scoped_service_credential():
        task = api("GET", f"/api/agent-access/v1/tasks/{args.task_id}")
        _emit(args, task, _render_agent_task_detail)
        return
    task = api("GET", f"/api/admin/tasks/{args.task_id}")
    _emit(args, task, _render_task_detail)


def _render_agent_task_detail(data: dict) -> None:
    """Render the bounded task projection used by scoped coding agents."""
    task = data.get("task") or {}
    status_data = data.get("status") or {}
    failure = data.get("failure_evidence") or {}
    print(
        f"=== {(task.get('id') or '?')[:8]} "
        f"[{task.get('task_type', '?')}] {task.get('status', '?')} ==="
    )
    print(f"  Title:   {task.get('title', '?')}")
    print(f"  Project: {task.get('project_id', '?')}")
    if task.get("pr_url"):
        print(f"  PR:      {task['pr_url']}")
    if status_data.get("latest_message"):
        print(f"  Latest:  {status_data['latest_message']}")
    blocker = failure.get("blocked_reason") or failure.get("park_reason")
    if blocker:
        print(f"  Blocker: {blocker}")
    error_tail = failure.get("error_log_tail")
    if error_tail:
        print("\n  Failure evidence:")
        print(f"    {error_tail}")
    truncated = [
        section for section, value in (data.get("truncation") or {}).items() if value
    ]
    if truncated:
        print(f"\n  Truncated sections: {', '.join(truncated)}")


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


def _resolved_failure_class(t: dict) -> str:
    """Return the persisted failure class for a task dict, most canonical first.

    Three fields carry a failure class on the admin task projection and they are
    NOT interchangeable:

    * ``context['failure_category']`` -- the canonical one. Every terminal-FAILED
      code path is required to stamp it via
      ``failure_classification.persist_failure_category``, so this is the field
      that is actually populated on the estate.
    * ``context['failure_class']`` -- the authoritative-verdict cache written by
      ``persist_authoritative_verdict``. Only the reaper writes it, so it is
      present on a small minority of rows.
    * top-level ``failure_class`` -- the server-side ``classify_failure(task)``
      projection, derived from log text at read time. A useful last resort, but
      the endpoint only computes it for FAILED/CANCELLED rows that have a status
      message.

    Reading only ``context['failure_class']`` therefore reports nothing for most
    genuinely classified failures -- state without reason, which is the exact
    thing the operator surfaces are supposed to stop doing.
    """
    ctx = t.get("context") or {}
    return (
        ctx.get("failure_category")
        or ctx.get("failure_class")
        or t.get("failure_class")
        or ""
    )


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
        fc = _resolved_failure_class(t)
        detail = ctx.get("failure_detail") or ""

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
        elif detail:
            # No class at all, but the terminalizing path left a finer-grained
            # marker. Surfacing it beats reporting UNKNOWN: the operator gets a
            # name to grep for instead of a shrug.
            cause = f"DETAIL:{detail}"

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
                    # The persisted reason itself, not just the derived bucket:
                    # a bucket says which pile the row landed in, these say what
                    # the terminalizing path actually recorded on the row.
                    "failure_class": _resolved_failure_class(t) or None,
                    "failure_detail": (t.get("context") or {}).get(
                        "failure_detail"
                    ),
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
            reason_bits = [
                part for part in (t.get("failure_class"), t.get("failure_detail"))
                if part
            ]
            if reason_bits:
                print(f"    WHY: {' / '.join(reason_bits)}")
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
    fc = _resolved_failure_class(t)
    if fc and t.get("status") in ("FAILED", "CANCELLED"):
        print(f"  FailClass: {fc}")
    fdetail = ctx.get("failure_detail")
    if fdetail and t.get("status") in ("FAILED", "CANCELLED"):
        print(f"  FailDetail: {fdetail}")
    # Why a task is not moving is the first thing an operator asks, and it was
    # the one thing this view never answered -- the fields were in the JSON and
    # absent from the human render, so `tasks show` reported the state and
    # withheld the reason.
    blocked_reason = t.get("blocked_reason")
    if blocked_reason:
        kind = t.get("blocker_kind")
        blocked_at = (t.get("blocked_at") or "")[:19]
        suffix = " ".join(
            part for part in (f"[{kind}]" if kind else "",
                              f"since {blocked_at}" if blocked_at else "")
            if part
        )
        print(f"  Blocked:  {blocked_reason}"[:300]
              + (f"  {suffix}" if suffix else ""))
        if t.get("blocker_escalated"):
            print("    (escalated to a human)")
    park_reason = t.get("park_reason")
    if park_reason:
        parked_at = (t.get("parked_at") or "")[:19]
        print(f"  Parked:   {park_reason}"[:300]
              + (f"  since {parked_at}" if parked_at else ""))
    if t.get("requires_approval"):
        print("  Approval: REQUIRED (waiting on a human decision)")
    depends_on = t.get("depends_on") or []
    if depends_on:
        print(f"  Depends:  {', '.join(str(d)[:8] for d in depends_on)}")
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
            "read-only. Select a separate operator BLUEPRINT_CONFIG; do not use "
            "BLUEPRINT_AGENT_CONFIG or a coding-agent credential to mutate.",
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
    scoped = _using_scoped_service_credential()
    if scoped:
        forbidden = {
            "--repo": getattr(args, "repo", None),
            "--base-branch": getattr(args, "base_branch", None),
            "--model": getattr(args, "model", None),
            "--effort": getattr(args, "effort", None),
            "--hotfix": getattr(args, "hotfix", False),
            "--require-deps-landed-on": getattr(args, "require_deps_landed_on", None),
            "--release-base-branch": getattr(args, "release_base_branch", None),
            "--acceptance-evidence-file": getattr(
                args, "acceptance_evidence_file", None
            ),
        }
        requested = [flag for flag, value in forbidden.items() if value]
        if requested:
            print(
                "ERROR: project-operator task creation does not authorize "
                + ", ".join(requested)
                + ". Project/repository/base/provider authority is derived "
                "server-side, and scoped commands cannot read local evidence files.",
                file=sys.stderr,
            )
            raise APIError("task creation authority flag is not permitted")
    cfg = load_config()
    title = args.title
    prompt = args.prompt
    task_type = getattr(args, "task_type", "create")
    target_project_id = get_project_id(getattr(args, "project_id", None))
    github_repo = getattr(args, "repo", None) or cfg.get("default_repo", "")
    if not scoped and not getattr(args, "repo", None):
        selected_project = next(
            (
                project
                for project in list_projects()
                if str(project.get("id")) == str(target_project_id)
            ),
            None,
        )
        if selected_project is not None:
            owner = str(selected_project.get("github_owner") or "").strip()
            repo = str(selected_project.get("github_repo") or "").strip()
            if owner and repo:
                github_repo = f"{owner}/{repo}"
    base_branch = getattr(args, "base_branch", None)
    if not scoped and not github_repo:
        print("ERROR: --repo is required (or set default_repo in config).", file=sys.stderr)
        sys.exit(1)
    capability_route = (
        "/api/operator-access/v1/task-capabilities"
        if scoped
        else "/api/admin/task-capabilities"
    )
    capability_response = api("GET", capability_route)
    capability_contract = (
        capability_response.get("task_capabilities")
        if isinstance(capability_response, dict)
        else None
    )
    capability = (
        capability_contract.get(task_type)
        if isinstance(capability_contract, dict)
        else None
    )
    if not isinstance(capability, dict):
        print(
            f"ERROR: task type {task_type!r} is not in Blueprint's server capability registry.",
            file=sys.stderr,
        )
        raise APIError("unsupported task type")
    if not capability.get("user_launchable"):
        print(
            f"ERROR: task type {task_type!r} is not operator-launchable.",
            file=sys.stderr,
        )
        raise APIError("task type is not operator-launchable")
    evidence_options = [
        bool(getattr(args, "acceptance_test", None)),
        bool(getattr(args, "acceptance_behavior", None)),
        bool(getattr(args, "acceptance_evidence_file", None)),
    ]
    if sum(evidence_options) > 1:
        print(
            "ERROR: choose only one of --acceptance-test, --acceptance-behavior, "
            "or --acceptance-evidence-file.",
            file=sys.stderr,
        )
        raise APIError("multiple acceptance evidence declarations")
    acceptance_evidence = None
    if getattr(args, "acceptance_test", None):
        acceptance_evidence = {
            "kind": "named_test",
            "test_path": args.acceptance_test,
        }
    elif getattr(args, "acceptance_behavior", None):
        acceptance_evidence = {
            "kind": "observable_behavior",
            "description": args.acceptance_behavior,
        }
    elif getattr(args, "acceptance_evidence_file", None):
        evidence_path = Path(args.acceptance_evidence_file).expanduser()
        try:
            if evidence_path.stat().st_size > 64 * 1024:
                raise ValueError("file exceeds 64 KiB")
            acceptance_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(
                f"ERROR: cannot load acceptance evidence from {evidence_path}: {exc}",
                file=sys.stderr,
            )
            raise APIError("invalid acceptance evidence file") from exc
        if not isinstance(acceptance_evidence, dict):
            print("ERROR: acceptance evidence file must contain a JSON object.", file=sys.stderr)
            raise APIError("invalid acceptance evidence file")
    if capability.get("requires_acceptance_evidence") and acceptance_evidence is None:
        print(
            f"ERROR: code-producing task type {task_type!r} requires acceptance evidence. "
            "Pass --acceptance-test, --acceptance-behavior, or "
            "--acceptance-evidence-file.",
            file=sys.stderr,
        )
        raise APIError("acceptance evidence is required")
    if task_type in {"review", "fix"} and (
        getattr(args, "pr_number", None) is None or not getattr(args, "branch", None)
    ):
        print(
            f"ERROR: task type {task_type!r} requires --pr-number and --branch.",
            file=sys.stderr,
        )
        raise APIError("PR task metadata is required")
    body: dict = {"title": title, "prompt": prompt, "task_type": task_type}
    if not scoped:
        body["github_repo"] = github_repo
        body["project_id"] = target_project_id
    if not scoped and base_branch is not None:
        body["base_branch"] = base_branch
    if not scoped and getattr(args, "model", None):
        body["model"] = args.model
    if not scoped and getattr(args, "effort", None):
        body["effort"] = args.effort
    if not scoped and getattr(args, "hotfix", False):
        body["hotfix"] = True
    optional_fields = {
        "pr_number": getattr(args, "pr_number", None),
        "branch": getattr(args, "branch", None),
        "idempotency_key": getattr(args, "idempotency_key", None),
        "require_deps_landed_on": (
            None if scoped else getattr(args, "require_deps_landed_on", None)
        ),
        "release_base_branch": (
            None if scoped else getattr(args, "release_base_branch", None)
        ),
        "work_class": getattr(args, "work_class", None),
        "stop_or_pivot_condition": getattr(args, "stop_or_pivot_condition", None),
    }
    body.update({key: value for key, value in optional_fields.items() if value is not None})
    if acceptance_evidence is not None:
        body["acceptance_evidence"] = acceptance_evidence
    if not _confirm(f"Create task: {title[:60]}?", getattr(args, "yes", False)):
        print("Aborted.")
        return
    create_route = (
        f"/api/operator-access/v1/projects/{target_project_id}/tasks"
        if scoped
        else "/api/admin/tasks"
    )
    result = api("POST", create_route, body)
    _emit(args, result, lambda r: print(
        f"  task {'reused' if r.get('is_duplicate') else 'created'}: "
        f"{r.get('id', '?')}  [{r.get('task_type', '?')}]  {r.get('title', '')}"
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
    enable = getattr(args, "enable", False)
    disable = getattr(args, "disable", False)
    if enable or disable:
        if enable and disable:
            print("ERROR: --enable and --disable are mutually exclusive.",
                  file=sys.stderr)
            sys.exit(1)
        cfg = load_config()
        if cfg.get("read_only"):
            print(
                "ERROR: toggling autonomy is a mutating action but the resolved "
                "config is read-only (agent-JWT fallback). Provide an admin API "
                "key (BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
                file=sys.stderr,
            )
            sys.exit(1)
        pid = get_project_id(getattr(args, "project_id", None))
        target = bool(enable)
        if not _confirm(
            f"Set autonomous_enabled={target} on project {pid}?",
            getattr(args, "yes", False),
        ):
            print("Aborted.")
            return
        result = api("PATCH", f"/api/admin/projects/{pid}",
                     {"autonomous_enabled": target})
        _emit(args, result, lambda r: print(
            f"autonomous_enabled={r.get('autonomous_enabled', target)} "
            f"on project {pid}"))
        return
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
    elif sub == "resolve-discovery":
        _directions_resolve_discovery(args)
    elif sub == "show-discovery":
        _directions_show_discovery(args)
    else:
        _directions_list(args)


def _directions_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    if _using_scoped_service_credential():
        limit = getattr(args, "limit", 50)
        offset = getattr(args, "offset", 0)
        route = (
            f"/api/agent-access/v1/directions?project_id={pid}"
            f"&limit={limit}&offset={offset}"
        )
    else:
        route = f"/api/admin/projects/{pid}/directions"
    resp = api("GET", route)
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


def _direction_id_candidates(direction_id: str) -> list[str]:
    """Prefix forms a bare token may legitimately mean.

    Direction ids are ``d-<hex>``. Every surface that prints one (autonomy
    snapshots, review packets, task context) shows the hex, and roughly half
    of them show it WITHOUT the ``d-``. An operator who copies the hex out of
    one of those and pastes it here was, until this fix, told "Direction not
    found" for an id that exists — the help text says "id or prefix" and the
    resolver did a bare ``startswith``, so the two disagreed. Accept both
    spellings; the ambiguity guard below still refuses a token that matches
    more than one direction.
    """
    candidates = [direction_id]
    if not direction_id.startswith("d-"):
        candidates.append(f"d-{direction_id}")
    return candidates


def _resolve_direction(
    args: argparse.Namespace,
    direction_id: str,
    *,
    require_staged: bool = False,
) -> dict:
    pid = get_project_id(getattr(args, "project_id", None))
    candidates = _direction_id_candidates(direction_id)

    def _is_exact(did: str) -> bool:
        return did in candidates

    def _is_prefix(did: str) -> bool:
        return any(did.startswith(c) for c in candidates)

    matches: list[dict] = []
    scanned = 0
    contains: list[str] = []
    if _using_scoped_service_credential():
        offset = 0
        while offset < 10_000:
            resp = api(
                "GET",
                f"/api/agent-access/v1/directions?project_id={pid}"
                f"&limit=100&offset={offset}",
            )
            directions = resp.get("directions", []) if isinstance(resp, dict) else []
            scanned += len(directions)
            contains.extend(
                d.get("id") or "" for d in directions
                if direction_id and direction_id in (d.get("id") or "")
            )
            exact = [d for d in directions if _is_exact(d.get("id") or "")]
            if exact:
                matches = exact
                break
            matches.extend(d for d in directions if _is_prefix(d.get("id") or ""))
            next_offset = resp.get("next_offset") if isinstance(resp, dict) else None
            if not resp.get("has_more") or not isinstance(next_offset, int):
                break
            if next_offset <= offset:
                raise APIError("direction pagination made no progress")
            offset = next_offset
        else:
            raise APIError(
                "direction catalog exceeds the 10,000-row CLI resolution bound; "
                "use the exact direction id"
            )
    else:
        resp = api("GET", f"/api/admin/projects/{pid}/directions")
        directions = resp.get("directions", []) if isinstance(resp, dict) else resp
        scanned = len(directions)
        contains = [
            d.get("id") or "" for d in directions
            if direction_id and direction_id in (d.get("id") or "")
        ]
        matches = [d for d in directions if _is_prefix(d.get("id") or "")]
    if not matches:
        # Surface the reason, not just the state: an operator who gets
        # "not found" for an id they just read off a snapshot has no way to
        # tell a typo from a wrong project from a retired direction.
        print(f"Direction not found: {direction_id}", file=sys.stderr)
        print(
            f"  Searched {scanned} direction(s) in project {pid}.",
            file=sys.stderr,
        )
        near = sorted(set(i for i in contains if i))[:5]
        if near:
            print("  Ids containing that token:", file=sys.stderr)
            for i in near:
                print(f"    {i}", file=sys.stderr)
        else:
            print(
                "  Direction ids are 'd-<hex>'; both 'd-<hex>' and a bare "
                "'<hex>' prefix resolve. Run 'directions list --all' to see "
                "retired and held directions this project still carries.",
                file=sys.stderr,
            )
        sys.exit(1)
    if len(matches) > 1:
        print(f"Direction prefix is ambiguous: {direction_id}", file=sys.stderr)
        for d in sorted(matches, key=lambda x: x.get("id") or "")[:10]:
            print(f"    {d.get('id', '?')}  {d.get('status', '?')}", file=sys.stderr)
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
    idempotency_key = getattr(args, "idempotency_key", None)
    body: dict = {"text": text, "priority": priority}
    if paths:
        body["paths"] = paths
    if completion_criteria:
        body["completion_criteria"] = completion_criteria
    if staged:
        body["staged"] = True
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    if not _confirm(f"Add {'staged ' if staged else ''}direction: {text[:60]}?",
                    getattr(args, "yes", False)):
        print("Aborted.")
        return
    pid = get_project_id(getattr(args, "project_id", None))
    route = (
        f"/api/operator-access/v1/projects/{pid}/directions"
        if _using_scoped_service_credential()
        else f"/api/admin/projects/{pid}/directions"
    )
    result = api("POST", route, body)
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

    scoped = _using_scoped_service_credential()
    if scoped and (
        getattr(args, "status", None)
        or getattr(args, "staged_for_version", None) is not None
        or getattr(args, "reset_context", False)
        or getattr(args, "reset_history", False)
        or getattr(args, "reset_agent_notes", False)
        or getattr(args, "reset_all", False)
    ):
        print(
            "ERROR: project-operator directions update permits only text, priority, "
            "paths, and completion criteria. Use activate/retarget/cancel for "
            "staged lifecycle actions; "
            "research/history resets remain admin-only.",
            file=sys.stderr,
        )
        raise APIError("direction update field is not permitted")

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
    if getattr(args, "paths", None) is not None:
        patch["paths"] = args.paths
    if getattr(args, "completion_criteria", None) is not None:
        patch["completion_criteria"] = args.completion_criteria
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
        print("Nothing to update. Use --text, --priority, --paths, "
              "--completion-criteria, --status, --staged-for-version, or a reset flag.",
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

    route = (
        f"/api/operator-access/v1/projects/{pid}/directions/{did}"
        if scoped
        else f"/api/admin/projects/{pid}/directions/{did}"
    )
    result = api("PATCH", route, patch)
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
    if _using_scoped_service_credential():
        result = api(
            "POST",
            f"/api/operator-access/v1/projects/{pid}/directions/{did}/retarget",
            {"version": version},
        )
    else:
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
    if _using_scoped_service_credential():
        result = api(
            "POST",
            f"/api/operator-access/v1/projects/{pid}/directions/{did}/activate",
            {},
        )
    else:
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
    if _using_scoped_service_credential():
        result = api(
            "POST",
            f"/api/operator-access/v1/projects/{pid}/directions/{did}/cancel",
            {},
        )
    else:
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


_NEXT_BET_FIELDS = {
    "mode": "bet_mode",
    "claim": "bet_claim",
    "decision_question": "bet_question",
    "success_evidence": "bet_success",
    "retirement_condition": "bet_retirement",
}


def _directions_resolve_discovery(args: argparse.Namespace) -> None:
    """Resolve an ask_operator/reviewed discovery cycle from the CLI."""
    cfg = load_config()
    if cfg.get("read_only"):
        print(
            "ERROR: 'resolve-discovery' is a mutating action but the resolved "
            "config is read-only (agent-JWT fallback). Provide an admin API key "
            "(BLUEPRINT_API_KEY / .blueprint-config.json) to mutate.",
            file=sys.stderr,
        )
        sys.exit(1)
    pid = get_project_id(getattr(args, "project_id", None))
    d = _resolve_direction(args, args.direction_id)
    did = d["id"]
    action = args.action
    bet_values = {key: getattr(args, attr, None)
                  for key, attr in _NEXT_BET_FIELDS.items()}
    provided = [k for k, v in bet_values.items() if v]
    if action == "continue":
        missing = [k for k, v in bet_values.items() if not v]
        if missing:
            print(
                "ERROR: continue requires the full next-bet contract; missing: "
                + ", ".join(f"--{_NEXT_BET_FIELDS[k].replace('_', '-')}"
                            for k in missing),
                file=sys.stderr,
            )
            sys.exit(1)
        next_bet = bet_values
    else:
        if provided:
            print(f"ERROR: {action} must not include next-bet fields.",
                  file=sys.stderr)
            sys.exit(1)
        next_bet = None
    payload = {
        "cycle_id": args.cycle_id,
        "cycle_revision": args.cycle_revision,
        "action": action,
    }
    if next_bet is not None:
        payload["next_bet"] = next_bet
    if getattr(args, "note", None):
        payload["note"] = args.note
    if not _confirm(
        f"Resolve discovery cycle {args.cycle_id} on {did} with '{action}'?",
        getattr(args, "yes", False),
    ):
        print("Aborted.")
        return
    result = api(
        "POST",
        f"/api/admin/projects/{pid}/directions/{did}/discovery-resolution",
        payload,
    )
    _emit(args, result, lambda r: print(
        f"Resolved cycle {args.cycle_id} on {did}: action={action}"
        + (f", successor cycle {r.get('next_cycle_id')}"
           if r.get("next_cycle_id") else "")
    ))


def _directions_show_discovery(args: argparse.Namespace) -> None:
    """Show a direction's most recent discovery cycles: state, verdict,
    operator_question, and binding-decision authority.

    Read-only counterpart to ``resolve-discovery`` — it exists so an operator
    can see WHY a cycle landed where it did (and get the cycle_id +
    cycle_revision resolve-discovery needs) without DB/SSH access.
    """
    pid = get_project_id(getattr(args, "project_id", None))
    d = _resolve_direction(args, args.direction_id)
    did = d["id"]
    limit = getattr(args, "limit", 5)
    result = api(
        "GET",
        f"/api/admin/projects/{pid}/directions/{did}/discovery-cycles"
        f"?limit={limit}",
    )
    _emit(args, result, _render_discovery_cycles)


def _render_discovery_cycles(data: dict) -> None:
    cycles = data.get("cycles") or []
    print(f"=== DISCOVERY CYCLES ({len(cycles)}) ===")
    if not cycles:
        print("  (none)")
        return
    for c in cycles:
        cid = c.get("id", "?")
        state = c.get("state", "?")
        revision = c.get("revision", "?")
        generation = c.get("generation", "?")
        verdict = c.get("verdict") or "-"
        allowed = c.get("binding_decision_allowed")
        allowed_str = "?" if allowed is None else ("yes" if allowed else "NO")
        updated = (c.get("updated_at") or "?")[:19]
        print(
            f"\n  {cid}  gen={generation}  rev={revision}  {state:16}  "
            f"verdict={verdict:10}  binding_allowed={allowed_str}  "
            f"updated={updated}"
        )
        summary = c.get("review_summary")
        if summary:
            print(f"    Summary: {summary}")
        question = c.get("operator_question")
        if question:
            print("    ** Operator question:")
            for line in textwrap.wrap(str(question), width=76):
                print(f"       {line}")
        bet = c.get("bet_snapshot") or {}
        claim = bet.get("claim")
        if claim:
            print(f"    Active bet: mode={bet.get('mode', '?')}  claim={claim}")


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
    limit = getattr(args, "limit", 50)
    offset = getattr(args, "offset", 0)
    scoped_status = status or "all"
    if _using_scoped_service_credential():
        result = api(
            "GET",
            f"/api/agent-access/v1/messages?project_id={pid}&status={scoped_status}"
            f"&limit={limit}&offset={offset}",
        )
    else:
        result = api(
            "GET",
            f"/api/admin/messages?project_id={pid}&status={scoped_status}"
            f"&limit={limit}&offset={offset}",
        )
    payload = result if isinstance(result, dict) else {"messages": result}
    _emit(args, payload, _render_messages)


_MSG_ICONS = {"BUG_REPORT": "BUG", "FEEDBACK": "FB", "NOTE": "NOTE",
              "QUESTION": "Q", "BLOCKER": "BLOCK"}

# ``status`` ("open"/"linked"/"resolved") is message RESOLUTION bookkeeping.
# ``handling_state`` is whether the engine actually took the message, and it is
# the field an operator needs: reaper step 25
# (reaper_bug_message_sweeps.sweep_fail_stranded_pending_conversation_messages)
# terminalizes an unclaimed conversation message to
# handling_state='failed' + status='resolved', so a message NOBODY read and a
# message that was answered both read as status='resolved'. Rendering only
# ``status`` is what made "sent, then silence" indistinguishable from "handled".
_HANDLING_LABELS = {
    "pending": "pending",
    "claimed": "claimed",
    "responded": "responded",
    "superseded": "superseded",
    "failed": "FAILED",
}


def _render_messages(data: dict) -> None:
    msgs = data.get("messages") or []
    if not msgs:
        print("No messages.")
        return
    for m in msgs:
        msg_id = m.get("id", "?")
        icon = _MSG_ICONS.get(m.get("type") or m.get("message_type", ""), "?")
        src = m.get("from") or m.get("from_type", "?")
        age = (m.get("created_at") or "?")[:16]
        raw_handling = m.get("handling_state")
        handling = _HANDLING_LABELS.get(raw_handling, raw_handling or "unreported")
        line = f"  {msg_id} [{icon}] ({src}, {age}, {handling})"
        print(f"{line} {(m.get('content') or '')[:120]}")
        if raw_handling == "failed":
            reason = m.get("resolution") or "no runner claimed this message"
            print(f"    ^ never handled: {reason}")


# ``arming`` is the server's verdict on whether ANYTHING will read the message
# just accepted (backend/app/strategic_trigger.py:94-141, 300-372). It is the
# ONLY field that separates a steer that reached the engine from a row that was
# merely stored, so a send receipt must never discard it: an operator message
# silently accepted into a lane nothing consumes is indistinguishable, at the
# terminal, from one that was acted on.
_ARMING_SERVICED = {
    "steering": "armed the strategist; the next strategic cycle consumes it",
    "resident_session": "delivered live to the running Workday session",
    "duplicate": "idempotent replay of an already-accepted message",
}
# Genuine steering whose marker could NOT be persisted. The row exists, but no
# strategic cycle will ever consume it.
_ARMING_UNSERVICED = "not_armed"
# Recorded for the message board, but by design these never arm a strategic
# cycle. Legitimate when intended, a silent drop when not.
_ARMING_PASSIVE = {
    "passive_note": "a passive NOTE never arms a strategic cycle",
    "routine_event": "a routine digest event never arms a strategic cycle",
}
_ARMING_REMEDY = (
    "use 'blueprint strategic send' or 'blueprint strategic chat', which open a "
    "Workday session that reads the conversation thread directly."
)


def _report_arming(result, *, lane: str) -> None:
    """Print the server's serviceability verdict; exit 1 when nothing will read it.

    ``lane`` is ``"messages"`` for the plain operator send and ``"strategic"``
    for the Workday send. The distinction is load-bearing, not cosmetic:

    * On the ``messages`` lane ``maybe_arm_operator_refresh`` is the SOLE gate
      (routers/admin_routers__messages.py). Nothing else on that path arranges a
      reader, so ``not_armed`` there is proof the message will never be read --
      a hard failure, not a warning.
    * The ``strategic`` lane additionally passes ``precreate_ensure``, which can
      bind the message to a live Workday session. Its arming outcome therefore
      does NOT decide readability on its own: a bound ``passive_note`` IS read,
      an unbound one is not. So report the verdict verbatim without claiming
      delivery, and never fail the send on the CLI's guess.

    Everything here goes to stderr: ``--json`` callers parse stdout, and they
    read the verdict from the ``arming`` key of the emitted receipt instead.
    """
    arming = (result or {}).get("arming") if isinstance(result, dict) else None
    if not arming:
        # Older server, or a route that does not report the verdict. Say so
        # rather than implying the message was picked up.
        print(
            "  (server did not report an arming outcome -- cannot confirm "
            "anything will read this message)",
            file=sys.stderr,
        )
        return
    if arming in _ARMING_SERVICED:
        print(f"  Handling: {arming} -- {_ARMING_SERVICED[arming]}", file=sys.stderr)
        return
    if lane == "strategic":
        if arming == _ARMING_UNSERVICED:
            print(
                "WARNING: arming=not_armed -- no strategic cycle was armed for "
                "this message. It is read only if the Workday session bound it, "
                "so check for a reply before assuming it landed.",
                file=sys.stderr,
            )
            return
        detail = _ARMING_PASSIVE.get(arming)
        suffix = f" -- {detail}" if detail else ""
        print(
            f"  Handling: {arming}{suffix}; delivery depends on the Workday "
            "session bound to this thread.",
            file=sys.stderr,
        )
        return
    if arming == _ARMING_UNSERVICED:
        print(
            "ERROR: this message was STORED but NOT armed: no strategic cycle "
            "will consume it and nothing else on this lane reads it, so nothing "
            "will ever answer it. Re-send to retry the steer, or "
            + _ARMING_REMEDY,
            file=sys.stderr,
        )
        sys.exit(1)
    if arming in _ARMING_PASSIVE:
        print(
            f"WARNING: {_ARMING_PASSIVE[arming]}, so nothing will act on this "
            "message -- it is recorded for the message board only. Send a "
            "steering type (--type FEEDBACK / QUESTION / BLOCKER) to arm the "
            "strategist, or " + _ARMING_REMEDY,
            file=sys.stderr,
        )
        return
    print(f"  Handling: {arming}", file=sys.stderr)


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
    body = {
        "content": args.content,
        "message_type": getattr(args, "type", "FEEDBACK"),
    }
    if getattr(args, "idempotency_key", None):
        body["idempotency_key"] = args.idempotency_key
    route = (
        f"/api/operator-access/v1/projects/{pid}/messages"
        if _using_scoped_service_credential()
        else f"/api/admin/projects/{pid}/messages"
    )
    result = api("POST", route, body)
    _emit(args, result, lambda d: print(f"  Message sent: {d.get('id', '?')}"))
    # Runs in --json mode too: the verdict decides the EXIT CODE, and a caller
    # that only checks the status line must not be told a dead send succeeded.
    _report_arming(result, lane="messages")


def _messages_answer(args: argparse.Namespace) -> None:
    _require_write_config("answer")
    pid = get_project_id(getattr(args, "project_id", None))
    message_id = args.message_id
    if _using_scoped_service_credential():
        result = api(
            "POST",
            f"/api/operator-access/v1/projects/{pid}/messages/{message_id}/answer",
            {"resolution": args.content},
        )
    else:
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
    """Dispatch fleet subcommands: pool / warm / accounts / zones / runners /
    grant / revoke."""
    sub = getattr(args, "fleet_cmd", None) or "pool"
    if sub == "warm":
        _fleet_warm(args)
    elif sub == "accounts":
        _fleet_accounts(args)
    elif sub == "zones":
        _fleet_zones(args)
    elif sub == "runners":
        _fleet_runners(args)
    elif sub == "grant":
        _fleet_grant(args)
    elif sub == "revoke":
        _fleet_revoke(args)
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


def _fleet_list_runners(project_id: str | None = None) -> list:
    """Collect runner records, de-duplicated by runner id.

    With ``project_id`` set, lists only the runners reachable from that project
    (``GET /api/admin/projects/{id}/runners`` returns runners enrolled in OR
    bound to it). Without one, sweeps every authorized project — necessary for
    ``fleet grant``, because the project you are about to bind cannot see the
    runner yet, by definition.
    """
    if project_id:
        scopes = [str(project_id)]
    else:
        scopes = [
            str(p.get("id")) for p in (list_projects() or []) if p.get("id")
        ]
    found: dict = {}
    for pid in scopes:
        # limit defaults to 50 server-side; ask for the documented maximum so a
        # large fleet is not silently truncated in the listing.
        rows, err = _safe_read(f"/api/admin/projects/{pid}/runners?limit=1000")
        if err or not isinstance(rows, list):
            continue
        for row in rows:
            runner_id = str((row or {}).get("id") or "")
            if runner_id and runner_id not in found:
                found[runner_id] = row
    return list(found.values())


def _fleet_runner_projects_path(runner: dict, suffix: str = "") -> str:
    """Build a runner allow-list sub-resource path.

    IMPORTANT — do not "simplify" this to the selected/listing project id. The
    runner allow-list sub-resources are keyed by the runner's ENROLLMENT project
    (``LocalRunner.project_id``, surfaced on each runner record as
    ``enrollment_project_id``), not by whichever project you listed the runner
    from. The backend's ``_load_owned_runner`` filters
    ``LocalRunner.project_id == project_id``, so passing the listing project
    returns ``404 {"error": {"code": 404, "message": "Runner not found"}}`` even
    though the runner is plainly visible in that project's runner list.
    """
    enrollment = str(runner.get("enrollment_project_id") or "")
    runner_id = str(runner.get("id") or "")
    if not enrollment or not runner_id:
        raise APIError(
            "runner record is missing id/enrollment_project_id; cannot address "
            "its project allow-list"
        )
    return f"/api/admin/projects/{enrollment}/runners/{runner_id}/projects{suffix}"


def _fleet_match_runners(runners: list, query: str | None) -> list:
    """Match ``query`` against runner id (exact), hostname (exact), then id prefix."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    exact_id = [r for r in runners if str(r.get("id") or "").lower() == q]
    if exact_id:
        return exact_id
    by_hostname = [r for r in runners if str(r.get("hostname") or "").lower() == q]
    if by_hostname:
        return by_hostname
    return [r for r in runners if str(r.get("id") or "").lower().startswith(q)]


def _fleet_resolve_runner(runners: list, query: str | None) -> dict:
    """Resolve ``--runner`` (id, id prefix, or hostname) to exactly one runner.

    Hostnames are not unique across the fleet, so an ambiguous match fails loudly
    and prints every candidate rather than silently binding the wrong device.
    """
    matches = _fleet_match_runners(runners, query)
    if not matches:
        print(
            f"ERROR: no runner matches --runner {query!r}. "
            "Run `blueprint fleet runners` to list them.",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(matches) > 1:
        print(
            f"ERROR: --runner {query!r} is ambiguous — {len(matches)} runners match:",
            file=sys.stderr,
        )
        for r in matches:
            print(
                f"  {r.get('id')}  {r.get('hostname') or '?'}  "
                f"[{r.get('status') or '?'}]  enrollment={r.get('enrollment_project_id')}",
                file=sys.stderr,
            )
        print("Re-run with the runner id.", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def _fleet_binding_target(args: argparse.Namespace, action: str) -> str:
    """The project a grant/revoke acts on, taken from the global --project-id.

    ``--project-id`` / ``--project`` name the project whose access is being
    changed; the runner is named separately by ``--runner``. Required: there is
    no safe default for "which project should this runner serve".
    """
    target = getattr(args, "project_id", None)
    if not target:
        print(
            f"ERROR: `fleet {action}` requires --project-id <uuid> (or --project "
            f"<name>) — the project the runner should"
            f"{'' if action == 'grant' else ' no longer'} serve.",
            file=sys.stderr,
        )
        sys.exit(1)
    return str(target)


def _fleet_runners(args: argparse.Namespace) -> None:
    """List runners with hostname, status, enrollment project, and bindings."""
    runners = _fleet_list_runners(getattr(args, "project_id", None))
    rows = []
    for runner in runners:
        bound, err = _safe_read(_fleet_runner_projects_path(runner))
        rows.append({
            "id": runner.get("id"),
            "hostname": runner.get("hostname"),
            "status": runner.get("status"),
            "enrollment_project_id": runner.get("enrollment_project_id"),
            "projects": (bound or {}).get("projects") or [],
            "projects_error": err,
        })
    _emit(args, {"runners": rows, "total": len(rows)}, _render_fleet_runners)


def _render_fleet_runners(data: dict) -> None:
    runners = data.get("runners") or []
    print(f"=== FLEET RUNNERS ({len(runners)}) ===")
    if not runners:
        print("  (none)")
        return
    for r in runners:
        print(
            f"  {r.get('id')}  {r.get('hostname') or '?'}  "
            f"[{r.get('status') or '?'}]"
        )
        print(f"    enrollment: {r.get('enrollment_project_id')}")
        if r.get("projects_error"):
            print(f"    bound:      (unavailable: {r['projects_error']})")
            continue
        entries = r.get("projects") or []
        if not entries:
            print("    bound:      (none)")
        for entry in entries:
            tag = "  (enrollment)" if entry.get("is_enrollment") else ""
            print(f"    bound:      {entry.get('project_id')}{tag}")


def _render_runner_allow_list(data: dict) -> None:
    data = data or {}
    print(f"  runner:     {data.get('runner_id')}")
    print(f"  enrollment: {data.get('enrollment_project_id')}")
    for entry in data.get("projects") or []:
        tag = "  (enrollment)" if entry.get("is_enrollment") else ""
        print(f"  bound:      {entry.get('project_id')}{tag}")


def _fleet_grant(args: argparse.Namespace) -> None:
    """Add a project to a runner's allow-list so the fleet can dispatch to it."""
    _require_write_config("fleet grant")
    target = _fleet_binding_target(args, "grant")
    # Sweep every authorized project: the target project cannot see the runner
    # until this grant lands, so it can never be the discovery scope.
    runner = _fleet_resolve_runner(
        _fleet_list_runners(), getattr(args, "runner", None)
    )
    path = _fleet_runner_projects_path(runner)

    current = api("GET", path) or {}
    already = [
        e for e in (current.get("projects") or [])
        if str(e.get("project_id")) == target
    ]
    if already:
        note = " (enrollment anchor)" if already[0].get("is_enrollment") else ""
        print(
            f"Already bound: runner {runner.get('hostname') or runner.get('id')} "
            f"already serves {target}{note} — no change."
        )
        _emit(args, current, _render_runner_allow_list)
        return

    if not _confirm(
        f"Grant runner {runner.get('hostname') or runner.get('id')} "
        f"({runner.get('id')}) access to project {target}?",
        getattr(args, "yes", False),
    ):
        print("Aborted.")
        return

    result = api("POST", path, {"project_id": target})
    print(f"Granted: {target} -> runner {runner.get('id')}")
    _emit(args, result, _render_runner_allow_list)


def _fleet_revoke(args: argparse.Namespace) -> None:
    """Remove a project from a runner's allow-list."""
    _require_write_config("fleet revoke")
    target = _fleet_binding_target(args, "revoke")
    runner = _fleet_resolve_runner(
        _fleet_list_runners(), getattr(args, "runner", None)
    )
    path = _fleet_runner_projects_path(runner)
    enrollment = str(runner.get("enrollment_project_id") or "")

    current = api("GET", path) or {}
    entry = next(
        (e for e in (current.get("projects") or [])
         if str(e.get("project_id")) == target),
        None,
    )

    # The enrollment project is the runner's anchor membership, not a grant.
    # Removing it would orphan the runner (the backend rejects it 409); refuse
    # here so the operator gets the reason instead of an HTTP error.
    if target == enrollment or (entry is not None and entry.get("is_enrollment")):
        print(
            f"ERROR: {target} is runner {runner.get('id')}'s enrollment project, "
            "not an allow-list grant. Revoking it would orphan the runner; "
            "delete or re-enroll the runner instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    if entry is None:
        print(
            f"Not bound: runner {runner.get('hostname') or runner.get('id')} does "
            f"not serve {target} — no change."
        )
        _emit(args, current, _render_runner_allow_list)
        return

    if not _confirm(
        f"Revoke runner {runner.get('hostname') or runner.get('id')} "
        f"({runner.get('id')}) access to project {target}?",
        getattr(args, "yes", False),
    ):
        print("Aborted.")
        return

    result = api("DELETE", _fleet_runner_projects_path(runner, f"/{target}"))
    print(f"Revoked: {target} from runner {runner.get('id')}")
    _emit(args, result, _render_runner_allow_list)


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

    if qual_state_name == "promotion_check_wait":
        pr_label = f" #{promotion_pr_number}" if promotion_pr_number else ""
        lines.append(
            f"- Promotion PR{pr_label} is open and waiting for required checks - "
            "Blueprint revalidates the exact target and retries the merge; this cut will be rejected until it lands or is canceled."
        )

    lines.append("Proceed?")
    return "\n".join(lines)


def _force_promote_confirmation_prompt(pid: str, candidate_sha) -> str:
    """Build the force-promote confirmation prompt from lifecycle state.

    This is the most privileged lever in the CLI, so the prompt names the
    version, the exact commit that will be promoted, and the blocker being
    overridden. A failed lifecycle read degrades to an explicit "cannot show
    what will be promoted" line rather than a bare y/N — the operator must
    never be asked to confirm a force-promote with no disclosure at all.
    """
    lines = ["About to FORCE-PROMOTE this project's open version, SKIPPING the audit gate."]

    lifecycle, err = _safe_read(f"/api/admin/projects/{pid}/version-lifecycle")
    cv = {} if err else ((lifecycle or {}).get("current_version") or {})
    if err:
        lines.append(f"- Lifecycle read FAILED ({err}) - cannot show what will be promoted.")
    elif not cv:
        lines.append("- No open version reported; the backend will reject this with 404.")
    else:
        lines[0] = (
            f"About to FORCE-PROMOTE v{cv.get('version_number')}, SKIPPING the audit gate."
        )
        active_sha = cv.get("active_candidate_sha")
        if candidate_sha:
            lines.append(f"- Promoting the PINNED commit {candidate_sha}.")
            if active_sha and not (
                str(active_sha).startswith(str(candidate_sha))
                or str(candidate_sha).startswith(str(active_sha))
            ):
                lines.append(
                    f"  WARNING: that is NOT the candidate under evaluation ({active_sha})."
                )
        else:
            lines.append(
                "- Promoting the CURRENT integration-branch HEAD (no --candidate-sha pinned); "
                f"the candidate last evaluated was {active_sha or 'unknown'}."
            )
        lq = cv.get("last_qualification") or {}
        blocker = lq.get("blocker")
        if blocker:
            lines.append(
                f"- Overriding qualification blocker '{blocker}' "
                f"(status={lq.get('status')}, confidence={lq.get('confidence')}, "
                f"threshold={lq.get('threshold')})."
            )
        in_flight = cv.get("in_flight_task_count")
        if in_flight:
            lines.append(
                f"- {in_flight} in-flight task{'s' if in_flight != 1 else ''} will NOT be "
                "stopped and will NOT be part of this release."
            )

    lines.append(
        "- CI is NOT skipped: the promotion PR is still gated on the promotion target's "
        "required checks, and a RED run DELETES that PR and its release branch. Verify CI "
        "is green on the candidate BEFORE confirming."
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
    # OWNER-only. Skips the AUDIT gate, NOT CI: it still cuts release-vN, opens
    # the PR to the promotion target, and waits for that branch's required
    # checks. Exposed here because the alternative when a version latches is a
    # hand-assembled raw call against the admin API -- unconfirmed, unaudited and
    # easy to point at the wrong project. The backend rechecks OWNER role, so a
    # scoped `project-operator` credential gets 403 rather than this lever.
    "force-promote": ("POST", "/api/admin/projects/{pid}/promote/force",
                      "FORCE-PROMOTE the open version, SKIPPING the audit gate",
                      lambda a: {"candidate_sha": getattr(a, "candidate_sha", None),
                                 "reason": getattr(a, "reason", None)}),
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

    if action == "cut":
        prompt = _cut_confirmation_prompt(pid)
    elif action == "force-promote":
        prompt = _force_promote_confirmation_prompt(pid, getattr(args, "candidate_sha", None))
    else:
        prompt = f"About to {summary}. Proceed?"
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
    """Dispatch narrowly bounded system-intervention subcommands."""
    sub = getattr(args, "ops_cmd", None)
    if sub == "repair":
        _ops_repair(args)
    elif sub == "repair-fast-claim-redactions":
        _ops_repair_fast_claim_redactions(args)
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


def _ops_repair_fast_claim_redactions(args: argparse.Namespace) -> None:
    """Review or apply the owner-only false Fast-claim redaction repair."""

    # Even dry-run is owner-only. Refuse the agent-JWT/read-only fallback before
    # project resolution or any HTTP request; scoped ServiceCredentials are
    # rejected even earlier by _enforce_scoped_cli_command.
    _require_write_config("ops repair-fast-claim-redactions")
    mode = getattr(args, "fast_claim_repair_mode", None)
    if mode not in {"dry-run", "apply"}:
        print(
            "ERROR: ops repair-fast-claim-redactions requires dry-run or apply",
            file=sys.stderr,
        )
        raise APIError("fast-claim redaction repair mode is required")
    if mode == "apply" and not getattr(args, "yes", False):
        # argparse requires --yes, and this defense keeps direct callers safe.
        print(
            "ERROR: applying the Fast-claim redaction repair requires --yes.",
            file=sys.stderr,
        )
        raise APIError("fast-claim redaction repair apply requires --yes")

    project_id = get_project_id(getattr(args, "project_id", None))
    body = {
        "mode": "dry_run" if mode == "dry-run" else "apply",
        "window_start": args.window_start,
        "window_end": args.window_end,
        "buggy_serving_revision": args.buggy_serving_revision,
        "expected_serving_revision": args.expected_serving_revision,
    }
    if mode == "apply":
        body.update(
            {
                "operation_id": args.operation_id,
                "expected_candidate_count": args.expected_candidate_count,
                "expected_candidate_digest": args.expected_candidate_digest,
            }
        )

    result = api(
        "POST",
        f"/api/admin/projects/{project_id}/memory/repairs/fast-claim-redactions",
        body,
    )
    _emit(args, result, _render_fast_claim_redaction_repair)


def _render_fast_claim_redaction_repair(data: dict) -> None:
    mode = data.get("mode") or "?"
    action = "REVIEW" if mode == "dry_run" else "APPLY"
    print(f"=== FAST-CLAIM REDACTION REPAIR {action} ===")
    print(f"  Project:             {data.get('project_id', '?')}")
    print(f"  Serving revision:    {data.get('serving_revision', '?')}")
    if data.get("buggy_serving_revision"):
        print(f"  Buggy revision:      {data['buggy_serving_revision']}")
    if data.get("window_start") and data.get("window_end"):
        print(
            "  Window:              "
            f"{data['window_start']} .. {data['window_end']}"
        )
    print(f"  Candidate count:     {data.get('candidate_count', '?')}")
    print(f"  Candidate digest:    {data.get('candidate_digest', '?')}")
    if mode == "apply":
        print(f"  Operation id:        {data.get('operation_id', '?')}")
        print(f"  Restored:            {data.get('restored_count', '?')}")
        print(
            "  Reconsolidation due: "
            f"{data.get('reconsolidation_pending_count', '?')}"
        )
        print(f"  Queued job:          {data.get('queued_job_id') or '-'}")
        print(f"  Idempotent replay:   {bool(data.get('replayed', False))}")
    print(f"  Content returned:    {bool(data.get('content_returned', False))}")


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
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
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
            "authority": orch.get("authority"),
            "cycle_age_seconds": orch.get("cycle_age_seconds"),
            "cycle_age_kind": orch.get("cycle_age_kind"),
            "heartbeat_age_seconds": orch.get("heartbeat_age_seconds"),
            "stale_after_seconds": orch.get("stale_after_seconds"),
            "reason": orch.get("reason"),
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
    print("\n=== RING BUFFER (3-of-5 gate) ===")
    print(f"  Recovery: [{' | '.join(dots)}]  {streak}/{required} healthy")
    orch = data.get("orchestrator") or {}
    print("\n=== ORCHESTRATOR ===")
    print(f"  Status:    {orch.get('status', '?')}")
    if orch.get("authority"):
        print(f"  Authority: {orch['authority']}")
    heartbeat_age = orch.get("heartbeat_age_seconds")
    stale_after = orch.get("stale_after_seconds")
    if heartbeat_age is not None:
        warning = bool(stale_after is not None and heartbeat_age >= stale_after)
        print(
            f"  Last heartbeat: {int(heartbeat_age)}s ago"
            + ("  WARNING: heartbeat is stale" if warning else "")
        )
    cycle_age = orch.get("cycle_age_seconds")
    if cycle_age is None and heartbeat_age is None:
        print("  Last cycle: never (loop has not completed a cycle since process start)")
    elif cycle_age is not None:
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
    """Dispatch analytics subcommands: cost / duration / pause-stats / failing-types / platform-kills."""
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

    elif sub == "platform-kills":
        days = getattr(args, "days", 7)
        data, err = _safe_read(
            f"/api/admin/platform-kill-metrics?project_id={pid}&days={days}"
        )
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return
        _emit(args, data, _render_analytics_platform_kills)

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


def _render_analytics_platform_kills(data: dict) -> None:
    """Render the platform-kill rate, its class breakdown, and its unknowns.

    The unclassified share is printed next to the headline rate on purpose: a
    zero rate with a low classified fraction means "not measured", not "no
    defects", and an operator must not be able to read one without the other.
    """
    days = data.get("window_days", "?")
    kills = data.get("platform_kill_count", 0)
    per_day = data.get("platform_kills_per_day", 0.0)
    recovered = data.get("platform_kills_recovered", 0)
    terminal = data.get("terminal_task_count", 0)
    unclassified = data.get("unclassified_count", 0)
    frac = data.get("classified_fraction")

    print(f"=== PLATFORM KILLS (last {days}d, since {data.get('window_start','?')}) ===")
    print(f"  platform kills/day : {per_day}   ({kills} kills over {days}d)")
    print(f"    of which retried : {recovered}  (work continued under a replacement)")
    print(f"  terminal tasks     : {terminal}")
    print(f"  work outcomes      : {data.get('work_outcome_count', 0)}")
    print(f"  not-a-death        : {data.get('not_a_death_count', 0)}")

    if frac is None:
        print("  classified         : n/a (no terminal tasks in window)")
    else:
        print(f"  classified         : {frac:.1%}   ({unclassified} unclassified)")
        if frac < 0.9:
            print(
                f"  !! {1 - frac:.0%} of terminal tasks could not be classified — "
                "read the rate above as a FLOOR, not a measurement."
            )

    by_class = data.get("by_class") or []
    if by_class:
        print("  --- by class (WHY it counted as a platform kill) ---")
        for row in by_class:
            cls = row.get("kill_class", "?")
            n = row.get("count", 0)
            rec = row.get("recovered_count", 0)
            suffix = f", {rec} retried" if rec else ""
            print(f"  {cls:38} {n:4d}{suffix}")
            print(f"      why: {row.get('reason','')}")
    else:
        print("  (no platform kills classified in this window)")

    daily = data.get("daily") or []
    if daily:
        print("  --- per day (kills / terminal) ---")
        for row in daily:
            print(
                f"  {row.get('date','?')}  {row.get('platform_kills',0):4d}"
                f" / {row.get('terminal_tasks',0):4d}"
            )

    reasons = data.get("unclassified_reasons") or []
    if reasons:
        print("  --- unclassified: WHY we could not tell ---")
        for row in reasons:
            print(f"  {row.get('count',0):5d}x  {row.get('reason','')}")


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
    if _using_scoped_service_credential():
        result = api(
            "POST",
            f"/api/operator-access/v1/projects/{pid}/strategic-cycle",
            {"phase": phase},
        )
    else:
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
    """Dispatch resident Strategic session subcommands: start / stop / chat / send."""
    sub = getattr(args, "strategic_cmd", None)
    if sub == "start":
        _strategic_start(args)
    elif sub == "stop":
        _strategic_stop(args)
    elif sub == "chat":
        _strategic_chat(args)
    elif sub == "send":
        _strategic_send(args)
    else:
        print("Usage: blueprint strategic {start|stop|chat|send}", file=sys.stderr)
        sys.exit(1)


def _strategic_start(args: argparse.Namespace) -> None:
    """Start (or adopt) a resident Strategic session on the operator's runner."""
    _require_write_config("strategic start")
    pid = get_project_id(getattr(args, "project_id", None))
    body: dict = {}
    runner = getattr(args, "runner_id", None)
    if _using_scoped_service_credential() and runner:
        print(
            "ERROR: project-operator Strategic start uses server-selected placement; "
            "--runner-id is not authorized.",
            file=sys.stderr,
        )
        raise APIError("Strategic runner selection is not permitted")
    if runner:
        body["runner_id"] = runner
    if _using_scoped_service_credential():
        result = api(
            "POST", f"/api/operator-access/v1/projects/{pid}/strategic/start", {}
        )
        result = result.get("data") or result
    else:
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
        if _using_scoped_service_credential():
            resp = api(
                "GET",
                f"/api/operator-access/v1/projects/{pid}/strategic-conversation"
                "?limit=50",
            )
            return ((resp.get("data") or {}).get("items") or [])
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
                route = (
                    f"/api/operator-access/v1/projects/{pid}/strategic/messages"
                    if _using_scoped_service_credential()
                    else f"/api/admin/projects/{pid}/strategic-session/messages"
                )
                sent = api("POST", route, {
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


def _strategic_send(args: argparse.Namespace) -> None:
    """Send one non-interactive Strategic continuation message."""
    _require_write_config("strategic send")
    pid = get_project_id(getattr(args, "project_id", None))
    body = {
        "content": args.content,
        "message_type": getattr(args, "type", "NOTE"),
        "idempotency_key": (
            getattr(args, "idempotency_key", None)
            or f"strategic-send:{uuid.uuid4()}"
        ),
    }
    route = (
        f"/api/operator-access/v1/projects/{pid}/strategic/messages"
        if _using_scoped_service_credential()
        else f"/api/admin/projects/{pid}/strategic-session/messages"
    )
    result = api("POST", route, body)
    _emit(
        args,
        result,
        lambda row: print(f"  Strategic message sent: {row.get('id', '?')}"),
    )
    _report_arming(result, lane="strategic")


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


def _fmt_window_minutes(minutes) -> str:
    """Render a --include-finished window the way an operator said it."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "the default window"
    if m <= 0:
        return "0m (active jobs only)"
    if m >= 1440 and m % 1440 == 0:
        return f"{m // 1440}d"
    if m >= 60 and m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def _compute_criterion_cell(j: dict) -> str:
    """Render the SCIENTIFIC verdict, which is not the execution status.

    The backend derives ``criterion_status`` (NOT_DECLARED / MET / NOT_MET /
    UNMEASURED / INVALID) and serves it on every compute-job payload. Prefer it:
    ``criterion_verdict.met`` is a bool, so it collapses "the threshold was
    measured and missed" and "the metric was never measured at all" into the
    same ``met=False`` — and a job in the second case reads as a clean
    scientific failure when it is actually an unmeasured run.

    Falls back to the raw verdict for a pre-upgrade backend that does not serve
    the derived field.
    """
    cv = j.get("criterion_verdict") if isinstance(j.get("criterion_verdict"), dict) else None
    metric = cv.get("metric") if cv else None
    value = cv.get("value") if cv else None
    measurement = f" ({metric}={value})" if metric and value is not None else ""
    crit = j.get("criterion_status")
    if isinstance(crit, str) and crit:
        if crit == "NOT_DECLARED":
            return ""
        return f"  crit={crit}{measurement}"
    if cv is not None:
        return f"  met={cv.get('met')}{measurement}"
    return ""


def _compute_watchdog_cell(j: dict) -> str:
    """Surface an in-flight no-progress cut on the list row.

    ``watchdog_state == 'cutting'`` means the no-progress condition holds right
    now and the next poll pass terminates the job. An operator scanning the list
    should not have to open the job to learn that.
    """
    if j.get("status") not in _ACTIVE_COMPUTE_STATUSES:
        return ""
    if j.get("watchdog_state") != "cutting":
        return ""
    reason = j.get("watchdog_cut_reason") or "no_progress"
    return f"  ⚠ watchdog cutting ({reason})"


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


def cmd_compute(args: argparse.Namespace) -> int | None:
    """Dispatch compute subcommands: list / show / kill / analyze / run / launch."""
    sub = getattr(args, "compute_cmd", None) or "list"
    if sub == "kill":
        _compute_kill(args)
    elif sub == "analyze":
        _compute_analyze(args)
    elif sub == "show":
        _compute_show(args)
    elif sub == "run":
        # Propagate the local run's exit contract to the process exit code.
        return _compute_run_local(args)
    elif sub == "launch":
        _compute_launch(args)
    else:
        _compute_list(args)


def _compute_list(args: argparse.Namespace) -> None:
    pid = get_project_id(getattr(args, "project_id", None))
    limit = getattr(args, "limit", 20)
    status_filter = getattr(args, "status_filter", None)
    all_terminal = bool(getattr(args, "all_terminal", False))
    finished = getattr(args, "include_finished", 360)
    if all_terminal:
        resp = api("GET", f"/api/admin/projects/{pid}/compute-jobs"
                   f"?all_terminal=true&limit={limit}")
    else:
        resp = api("GET", f"/api/admin/projects/{pid}/compute-jobs"
                   f"?include_finished_minutes={finished}&limit={limit}")
    jobs = resp.get("compute_jobs", []) if isinstance(resp, dict) else resp
    has_more = resp.get("has_more", False) if isinstance(resp, dict) else False
    summary = resp.get("summary") if isinstance(resp, dict) else None
    if status_filter:
        wanted = status_filter.upper()
        # Match either the execution status or the derived experiment status, so
        # `--status CRITERION_UNMEASURED` is askable without the caller having to
        # know that those jobs are all filed under SUCCEEDED.
        jobs = [
            j for j in jobs
            if (j.get("status") or "").upper() == wanted
            or (j.get("experiment_status") or "").upper() == wanted
        ]
    _emit(
        args,
        {
            "compute_jobs": jobs,
            "has_more": has_more,
            "summary": summary,
            "status_filter": status_filter,
            "window": {
                "all_terminal": all_terminal,
                "include_finished_minutes": None if all_terminal else finished,
                "limit": limit,
            },
        },
        _render_compute_list,
    )


def _render_compute_list(data: dict) -> None:
    jobs = data.get("compute_jobs") or []
    print(f"=== COMPUTE JOBS ({len(jobs)}) ===")
    if not jobs:
        print("  (none)")
        # An empty list is a claim about a WINDOW, not about the project. Say
        # which window produced it, or the reader concludes there is no compute.
        window = data.get("window") or {}
        if window.get("all_terminal"):
            print("  (searched every terminal job; none matched)")
        else:
            print(
                "  (window: active jobs plus those finished in the last "
                f"{_fmt_window_minutes(window.get('include_finished_minutes'))} — "
                "widen with --include-finished <minutes> or --all-terminal)"
            )
        if data.get("status_filter"):
            print(f"  (filtered to status={data['status_filter']})")
        return
    now = datetime.now(timezone.utc)
    for j in jobs:
        jid = (j.get("id") or "?")[:8]
        status = j.get("status", "?")
        title = (j.get("task_title") or "")[:50]
        task_type = j.get("task_type") or "?"
        cost = j.get("final_cost_usd")
        cost_str = f"  ${cost:.2f}" if cost is not None else ""
        verdict_str = _compute_criterion_cell(j)
        exp = (j.get("experiment") or "")[:20]
        exp_str = f"  exp={exp}" if exp else ""
        burn = _compute_job_burn_rate(j, now)
        burn_str = f"  burn=${burn:.2f}/hr" if burn is not None else ""
        watchdog_str = _compute_watchdog_cell(j)
        terminal_reason = (j.get("terminal_reason") or "").strip()
        reason_str = (
            f"  reason={terminal_reason[:60]}"
            if terminal_reason and status not in _ACTIVE_COMPUTE_STATUSES
            else ""
        )
        print(
            f"  {jid}  {status:12} [{task_type}]  {title}"
            f"{cost_str}{verdict_str}{exp_str}{burn_str}{watchdog_str}{reason_str}"
        )
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
    # status is EXECUTION truth only. The backend fuses it with the criterion
    # verdict into experiment_status, which is what actually answers "did this
    # experiment work?" — a SUCCEEDED job can be CRITERION_UNMEASURED.
    experiment_status = j.get("experiment_status")
    if experiment_status and experiment_status != status:
        print(f"  result    : {experiment_status}")
    outcome = j.get("outcome")
    if outcome:
        print(f"  outcome   : {outcome}")
    criterion_status = j.get("criterion_status")
    if criterion_status and criterion_status != "NOT_DECLARED":
        print(f"  criterion : {criterion_status}")
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
        wd_state = j.get("watchdog_state")
        if wd_state:
            timeout = j.get("no_progress_timeout_seconds")
            t_str = f" (cut at {timeout}s no-progress)" if timeout else ""
            if wd_state == "cutting":
                reason = j.get("watchdog_cut_reason") or "no_progress"
                print(f"  watchdog  : ⚠ CUTTING NOW — {reason}{t_str}")
            elif wd_state == "protected":
                last = j.get("last_progress_at")
                last_str = f", last progress {last}" if last else ""
                print(f"  watchdog  : protected{t_str}{last_str}")
            else:
                inert = j.get("watchdog_inert_reason")
                inert_str = f" — {inert}" if inert else ""
                print(f"  watchdog  : {wd_state}{inert_str}{t_str}")
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
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
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
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
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

# ── `compute run` exit-code contract ─────────────────────────────────────────
# `compute run` is meant to gate loops and CI, so its process exit code has to
# distinguish the three outcomes an operator reacts to differently. Collapsing
# them to 0 (the pre-fix behaviour) makes the verb unscriptable.
_COMPUTE_EXIT_OK = 0                  # ran, measured, criterion MET (or none given)
_COMPUTE_EXIT_COMMAND_FAILED = 1      # the experiment command itself exited non-zero
_COMPUTE_EXIT_HARNESS_FAILED = 2      # nothing was measured: bad spec/env/result file
_COMPUTE_EXIT_CRITERION_NOT_MET = 3   # measured cleanly, criterion evaluated NOT MET

# Exit status the injected shell prologue uses to report that the Blueprint file
# contract never reached the experiment shell. Distinct from any status the
# harness itself returns so the reason can be classified exactly.
_COMPUTE_CONTRACT_BREACH_EXIT = 97

_COMPUTE_RUN_EXIT_CODE_HELP = """exit codes:
  0  ran, measured, and the success criterion was MET (or none was given)
  1  the experiment command itself exited non-zero
  2  harness failure: nothing was measured -- bad spec, a broken environment
     contract, or a missing/unparseable/metric-less result file. A run that
     declared a metric or a criterion and did not capture it NEVER exits 0
     and is NEVER reported SUCCEEDED.
  3  measured cleanly, but the success criterion evaluated NOT MET
"""


def _compute_arm_failure_reason(
    exit_code: int | None,
    declared_metric: str | None,
    *,
    result_missing: bool,
    metric_missing: bool,
    subject: str = "arm",
) -> str:
    if exit_code == _COMPUTE_CONTRACT_BREACH_EXIT:
        return (
            f"{subject} never received the Blueprint file contract "
            "(BLUEPRINT_RESULT_FILE was empty or unwritable in the experiment shell); "
            "see stderr for the exact breach"
        )
    if exit_code == 124:
        return f"{subject} timed out (exit 124)"
    if result_missing:
        if exit_code is not None and exit_code != 0:
            return f"{subject} crashed (exit {exit_code}): result file not written"
        return f"{subject} result file not written ({subject} may have failed)"
    if metric_missing:
        metric_name = declared_metric or "<unknown>"
        if exit_code is not None and exit_code != 0:
            return f"{subject} crashed (exit {exit_code}): metric '{metric_name}' not found in result"
        return f"unmeasured criterion: metric '{metric_name}' not found in result"
    if exit_code is not None and exit_code != 0:
        return f"{subject} crashed (exit {exit_code})"
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


# ── local experiment shell resolution ────────────────────────────────────────
# On Windows, ``subprocess.run(["bash", ...])`` is resolved by CreateProcess,
# whose documented search order puts ``%SystemRoot%\System32`` AHEAD of PATH.
# On any Windows host with the WSL optional component installed, the bare name
# therefore launches ``System32\bash.exe`` — the WSL launcher — even though
# ``shutil.which("bash")`` (PATH order only) reports Git Bash. The two disagree
# deterministically, and WSL does not inherit the Windows environment block, so
# BLUEPRINT_RESULT_FILE / BLUEPRINT_ARTIFACT_DIR / BLUEPRINT_LOCAL_GPU arrived
# EMPTY in the child and the Windows result path was unwritable from Linux: the
# run measured nothing while reporting SUCCEEDED. Resolve the interpreter
# explicitly, translate the contract paths for WSL, and carry the contract in
# the command string rather than trusting environment inheritance.

_COMPUTE_SHELL_POSIX = "posix"
_COMPUTE_SHELL_WSL = "wsl"


class _ShellPlan(NamedTuple):
    """How this host will launch the experiment shell."""

    exe: str             # "bash" on POSIX; an absolute interpreter path on Windows
    kind: str            # _COMPUTE_SHELL_POSIX | _COMPUTE_SHELL_WSL
    label: str           # human-readable description for diagnostics

    @property
    def argv(self) -> list[str]:
        """Argv prefix for an inline (``-lc``) program."""
        return [self.exe, "-lc"]

    @property
    def is_wsl(self) -> bool:
        return self.kind == _COMPUTE_SHELL_WSL


def _windows_path_to_wsl(path: str) -> str:
    """Translate a Windows path to the WSL ``/mnt/<drive>`` view of the same file.

    Returns the input unchanged when it is not a drive-letter path (UNC shares
    and already-POSIX paths) — the contract prologue then fails loudly in the
    child rather than the run silently measuring nothing.
    """
    # ntpath, not os.path: this translates a WINDOWS path and must give the
    # same answer on any host (the tests that pin it run on Linux CI too).
    absolute = ntpath.normpath(path) if ntpath.isabs(path) else os.path.abspath(path)
    normalized = absolute.replace("\\", "/")
    match = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if not match:
        return normalized
    drive, rest = match.group(1).lower(), match.group(2)
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def _compute_resolve_shell(*, _name: str | None = None, _isfile=None, _which=None) -> _ShellPlan:
    """Resolve the POSIX shell this host will actually launch, deterministically.

    Never returns the bare name ``bash`` on Windows: the whole defect class is
    that the bare name resolves differently from what ``shutil.which`` reports.
    """
    name = _name if _name is not None else os.name
    isfile = _isfile or os.path.isfile
    which = _which or shutil.which

    if name != "nt":
        return _ShellPlan("bash", _COMPUTE_SHELL_POSIX, "bash")

    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    # ntpath.join: this is a Windows path even when the guard runs on Linux CI.
    wsl_bash = ntpath.join(system_root, "System32", "bash.exe")
    if isfile(wsl_bash):
        # This is the binary CreateProcess would have picked for bare "bash".
        # Name it explicitly and treat it as WSL so the contract is translated.
        return _ShellPlan(wsl_bash, _COMPUTE_SHELL_WSL, f"WSL bash ({wsl_bash})")

    resolved = which("bash")
    if resolved:
        return _ShellPlan(resolved, _COMPUTE_SHELL_POSIX, f"bash ({resolved})")
    return _ShellPlan("bash", _COMPUTE_SHELL_POSIX, "bash (unresolved)")


# Runs inside the experiment shell before the operator's command. Turns a
# broken environment contract into an immediate, named failure with a distinct
# exit status instead of a command that runs and measures nothing.
_COMPUTE_CONTRACT_PROLOGUE = "\n".join(
    [
        '[ -n "${BLUEPRINT_RESULT_FILE:-}" ] || { printf "%s\\n" '
        '"blueprint: BLUEPRINT_RESULT_FILE did not reach the experiment shell" >&2; '
        f'exit {_COMPUTE_CONTRACT_BREACH_EXIT}; }}',
        ': > "$BLUEPRINT_RESULT_FILE.bpprobe" 2>/dev/null || { printf "%s\\n" '
        '"blueprint: cannot write the result file at $BLUEPRINT_RESULT_FILE from the experiment shell" >&2; '
        f'exit {_COMPUTE_CONTRACT_BREACH_EXIT}; }}',
        'rm -f "$BLUEPRINT_RESULT_FILE.bpprobe"',
    ]
)


def _compute_shell_command(
    plan: _ShellPlan,
    command: str,
    contract: dict[str, str],
    *,
    capture_environment: bool = True,
) -> str:
    """Build the shell program: contract exports (WSL only), guard, command, capture.

    Under WSL the Windows environment block is not inherited, so the contract is
    re-established with explicit ``export`` statements carrying ``/mnt``-
    translated paths. Everywhere else the exports are unnecessary — ``execve``
    carries ``env`` — but the guard still runs so a breach can never be silent.
    """
    lines: list[str] = []
    if plan.is_wsl:
        for key, value in contract.items():
            if value is None:
                continue
            translated = (
                _windows_path_to_wsl(value)
                if key in ("BLUEPRINT_RESULT_FILE", "BLUEPRINT_ARTIFACT_DIR")
                else value
            )
            lines.append(f"export {key}={shlex.quote(translated)}")
    lines.append(_COMPUTE_CONTRACT_PROLOGUE)
    if capture_environment:
        # Runs INSIDE the experiment shell, so what it records is the
        # environment that actually ran -- after setup_step, inside the
        # container. Installed before the command so it still fires when the
        # command exits the shell itself; see _COMPUTE_ENVIRONMENT_CAPTURE.
        lines.append(_COMPUTE_ENVIRONMENT_CAPTURE)
    lines.append(command)
    return "\n".join(lines)


def _compute_shell_invocation(
    plan: _ShellPlan,
    command: str,
    contract: dict,
    *,
    script_path: str,
) -> list[str]:
    """Build the full argv that runs *command* under *plan*.

    On POSIX the program is passed inline to ``bash -lc``. Under WSL it is NOT:
    the ``System32\\bash.exe`` launcher hands its ``-c`` string to WSL through a
    layer that expands ``$VAR`` ONE MORE TIME before bash ever parses it, so an
    inline program loses every variable it sets — even inside single quotes
    (``echo 'literal $HOME'`` prints the operator's home directory). Writing the
    program to a real script file and executing that file removes the extra
    expansion pass entirely, which is also what this repo's guidance on
    generating one language from another prescribes.
    """
    program = _compute_shell_command(plan, command, contract)
    if not plan.is_wsl:
        return [*plan.argv, program]
    with open(script_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(program + "\n")
    return [plan.exe, "-l", _windows_path_to_wsl(script_path)]


def _compute_arm_slug(name: str) -> str:
    """Filesystem-safe per-arm token for result files and artifact directories."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", str(name)).strip("._-")
    return slug or "arm"


def _compute_run_exit_code(
    *,
    exit_code: int | None,
    measurement_failed: bool,
    verdict: dict | None,
) -> int:
    """Map a finished local run onto the documented `compute run` exit contract."""
    if exit_code == _COMPUTE_CONTRACT_BREACH_EXIT:
        return _COMPUTE_EXIT_HARNESS_FAILED
    if exit_code is not None and exit_code != 0:
        return _COMPUTE_EXIT_COMMAND_FAILED
    if measurement_failed:
        return _COMPUTE_EXIT_HARNESS_FAILED
    if verdict:
        if verdict.get("verdict") == "NOT MET":
            return _COMPUTE_EXIT_CRITERION_NOT_MET
        if verdict.get("verdict") != "MET":
            return _COMPUTE_EXIT_HARNESS_FAILED
    return _COMPUTE_EXIT_OK


def _render_criterion_verdict(verdict: dict | None) -> None:
    """Human render of a success-criterion verdict, shared by both run paths."""
    if not verdict:
        return
    vstr = verdict["verdict"]
    val = verdict.get("value")
    op = verdict.get("operator")
    thr = verdict.get("threshold")
    mn = verdict.get("metric")
    arm = verdict.get("arm")
    suffix = f" [arm {arm}]" if arm else ""
    if val is not None:
        print(f"verdict     : {vstr}  ({mn}={val} {op} {thr}){suffix}")
    else:
        print(f"verdict     : {vstr}  ({mn} not evaluable){suffix}")


def _run_capturing_stderr(argv: list[str], *, env=None, cwd=None, stderr_path: str):
    """Run *argv* with stderr both streamed live and captured to *stderr_path*.

    The arms path already captured stderr into a file so a failed arm could
    report a ``stderr_excerpt``; the single-command path had no diagnostics at
    all. Capturing through an ``os.pipe`` pumped by a reader thread gives the
    single-command path the same excerpt WITHOUT taking away the live stderr an
    operator watching a training loop depends on (logging and tqdm both write
    there). ``subprocess.run`` is kept as the launcher so the call site stays
    interchangeable with the rest of this module.
    """
    read_fd, write_fd = os.pipe()

    def _pump() -> None:
        try:
            with os.fdopen(read_fd, "rb", closefd=True) as reader:
                for line in reader:
                    try:
                        sink.write(line)
                    except Exception:  # noqa: BLE001 — never let capture kill the run
                        pass
                    try:
                        sys.stderr.buffer.write(line)
                        sys.stderr.flush()
                    except Exception:  # noqa: BLE001 — a closed/replaced stderr is not fatal
                        pass
        except Exception:  # noqa: BLE001
            pass

    with open(stderr_path, "wb") as sink:
        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()
        try:
            proc = subprocess.run(argv, env=env, cwd=cwd, stderr=write_fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(write_fd)
            pump.join(timeout=10)
    return proc


def _evaluate_criterion_across_arms(arm_metrics_by_name: dict, criterion: dict) -> dict:
    """Evaluate one success criterion over every arm of a local sweep.

    A sweep asks "does any arm clear the bar", so the overall verdict is MET
    when at least one arm meets it. Per-arm verdicts are kept under ``arms`` so
    a NOT MET sweep still shows which arm came closest to being evaluable.
    """
    per_arm = {
        name: _evaluate_criterion(metrics, criterion)
        for name, metrics in arm_metrics_by_name.items()
    }
    met = [name for name, v in per_arm.items() if v.get("met")]
    evaluable = [name for name, v in per_arm.items() if v.get("verdict") in ("MET", "NOT MET")]
    if met:
        chosen, verdict = met[0], "MET"
    elif evaluable:
        chosen, verdict = evaluable[0], "NOT MET"
    else:
        chosen, verdict = None, "NOT EVALUABLE"
    base = dict(per_arm[chosen]) if chosen else dict(_evaluate_criterion(None, criterion))
    base["verdict"] = verdict
    base["arm"] = chosen
    base["arms"] = per_arm
    return base


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


# ── local environment contract: images, spec fields, realised environment ────
# The gaps this block closes all share one shape: the local path QUIETLY did
# something other than what the spec said.
#
#   * ``docker pull`` ran unconditionally and a failed pull was fatal, so an
#     image BUILT ON THIS MACHINE -- the ordinary way to get a clean, pinned
#     environment onto your own hardware -- could never be used: a tag that
#     exists in no registry can never be pulled.
#   * ``setup_step`` is part of the compute-experiment payload and the local
#     path dropped it on the floor, so a run whose dependencies came from the
#     setup step measured nothing and blamed the command.
#   * nothing recorded what actually ran, so a finished run could not be
#     reproduced from what was written down.
_COMPUTE_PULL_POLICIES = ("missing", "always", "never")
_COMPUTE_DOCKER_INSPECT_TIMEOUT = 20

# Written by the experiment shell ITSELF (see _COMPUTE_ENVIRONMENT_CAPTURE) so
# the capture reflects the environment AFTER setup_step ran, not a pristine
# second container that never saw it.
_COMPUTE_PACKAGES_FILENAME = "blueprint_python_packages.txt"
_COMPUTE_ENVIRONMENT_FILENAME = "blueprint_environment.json"
_COMPUTE_MAX_RECORDED_PACKAGES = 4000

# Installed as an EXIT trap BEFORE the command rather than appended after it.
# An experiment command that ends in `exit 1` -- or that any `set -e` aborts --
# terminates the shell on the spot, so an appended epilogue would simply never
# run and the environment of exactly the runs you most want to explain would go
# unrecorded. The trap fires on every exit path.
#
# $? is captured on the first line of the handler and returned on the last, so
# the recording can never change the status the experiment reported.
_COMPUTE_ENVIRONMENT_CAPTURE = "\n".join(
    [
        "__bp_capture_environment() {",
        "  __bp_status=$?",
        '  if [ -n "${BLUEPRINT_ARTIFACT_DIR:-}" ]; then',
        '    mkdir -p "$BLUEPRINT_ARTIFACT_DIR" 2>/dev/null',
        "    { python3 -m pip freeze || python -m pip freeze || pip freeze; } "
        '> "$BLUEPRINT_ARTIFACT_DIR/' + _COMPUTE_PACKAGES_FILENAME + '" 2>/dev/null',
        "  fi",
        "  return $__bp_status",
        "}",
        "trap __bp_capture_environment EXIT",
    ]
)

# Spec fields the LOCAL path reads.
_COMPUTE_LOCAL_SPEC_HONOURED = frozenset({
    "command", "arms", "declared_metric", "success_criterion", "setup_step",
    "container_image", "container_registry_auth", "env", "cwd", "repo_clone",
})

# Cloud ComputeExperimentPayload fields that describe how the PLATFORM
# provisions, bills, or annotates a run. A local run provisions nothing, so
# they cannot apply -- but every one of them is NAMED on the way past rather
# than dropped in silence.
_COMPUTE_LOCAL_SPEC_CLOUD_ONLY = frozenset({
    "accelerator", "backend_name", "benchmark", "container_image_digest_pinned",
    "deadline_seconds", "experiment", "hypothesis", "live_proof",
    "live_proof_marker", "machine_type", "metric", "no_progress_timeout_seconds",
    "provisioning_model", "repo_token", "require_durable_artifact",
    "spend_cap_usd", "sweep_id",
})

# Fields that change WHAT IS MEASURED or WHAT CODE RUNS. Ignoring one of these
# yields a confidently wrong answer, so the local run refuses BY NAME instead
# of quietly running a different experiment.
_COMPUTE_LOCAL_SPEC_UNSUPPORTED = {
    "success_criteria": (
        "declares measurements this local runner does not evaluate; pass the "
        "one criterion as 'success_criterion' instead"
    ),
    "input_artifact_job_id": (
        "rehydrates a prior cloud job's artifact bundle, which a local run "
        "cannot fetch; the command would start from an empty workspace"
    ),
}


def _compute_spec_field_report(spec: dict) -> "tuple[list[str], list[str]]":
    """Split spec keys into (refuse, report-and-continue).

    Nothing falls through unnamed: a key is either honoured, fatal, or
    reported.
    """
    fatal: list[str] = []
    ignored: list[str] = []
    for key in spec:
        if key in _COMPUTE_LOCAL_SPEC_HONOURED:
            continue
        if key in _COMPUTE_LOCAL_SPEC_UNSUPPORTED:
            fatal.append(str(key))
        else:
            ignored.append(str(key))
    return sorted(fatal), sorted(ignored)


def _compute_spec_ignored_warning(key: str) -> str:
    if key in _COMPUTE_LOCAL_SPEC_CLOUD_ONLY:
        return (
            f"warning: spec field {key!r} configures a CLOUD launch and has no "
            f"effect on a local run"
        )
    return (
        f"warning: spec field {key!r} is not read by `blueprint compute run` "
        f"and was ignored"
    )


def _compute_fold_setup_step(command: str, setup_step: "str | None") -> str:
    """Run *setup_step* before *command*, in the SAME shell and boundary.

    Matches ``local_runner._run_compute_job``: brace groups joined by ``&&`` so
    a failed setup aborts before the command and surfaces the setup's exit
    code, and so an ``export`` or a ``cd`` in the setup step is visible to the
    command.
    """
    if not setup_step:
        return command
    return "{\n" + setup_step + "\n} && {\n" + command + "\n}"


def _compute_image_repo_name(image: str) -> str:
    """Strip tag and digest from an image reference, leaving the repository."""
    ref = image.split("@", 1)[0]
    head, sep, tail = ref.rpartition(":")
    # A tag can never contain "/", so a colon after the last slash is a tag and
    # a colon before one is a registry port.
    if sep and "/" not in tail:
        return head
    return ref


def _compute_inspect_local_image(image: str, *, _subprocess_run=None) -> "dict | None":
    """Return the realised identity of *image* when it is already on this host.

    ``None`` means "not usable from the local image store" -- including when
    docker itself is absent -- so the caller falls through to the pull path and
    reports docker's own error instead of a synthesized one.
    """
    _run = _subprocess_run if _subprocess_run is not None else subprocess.run
    try:
        proc = _run(
            [
                "docker", "image", "inspect", image,
                "--format", "{{.Id}}|{{json .RepoDigests}}",
            ],
            capture_output=True, text=True,
            timeout=_COMPUTE_DOCKER_INSPECT_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 -- no docker means "not present", not a crash
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    # getattr, not proc.stdout: a caller may hand this a stub result object,
    # and "could not read the image identity" is a fallthrough, not a crash.
    lines = [ln for ln in (getattr(proc, "stdout", "") or "").splitlines() if ln.strip()]
    if not lines:
        return None
    image_id, _, raw_digests = lines[0].strip().partition("|")
    try:
        repo_digests = json.loads(raw_digests) if raw_digests else []
    except ValueError:
        repo_digests = []
    if not isinstance(repo_digests, list):
        repo_digests = []
    return {
        "id": image_id or None,
        "repo_digests": [d for d in repo_digests if isinstance(d, str)],
    }


def _compute_realised_image_digest(image: str, identity: dict) -> "str | None":
    """The digest that makes this exact image re-runnable.

    A repo digest (``repo@sha256:...``) is preferred because another host can
    pull it. The local image id is the fallback for an image that was built
    here and pushed nowhere -- still worth recording, because it pins what ran.
    """
    repo = _compute_image_repo_name(image)
    digests = identity.get("repo_digests") or []
    for digest in digests:
        if digest.split("@", 1)[0] == repo:
            return digest
    if digests:
        return digests[0]
    return identity.get("id")


def _compute_resolve_image(
    image: str,
    *,
    policy: str = "missing",
    registry_auth: "dict | None" = None,
    quiet: bool = False,
    _subprocess_run=None,
    _inspect=None,
    _login=None,
) -> dict:
    """Make *image* runnable on this host and describe what was resolved.

    THE FIX: a local image is preferred when it is already here, a pull happens
    only when one is needed, and a failed pull is fatal ONLY when there is no
    usable image. ``docker pull`` of a locally built tag fails by definition --
    it exists in no registry -- and that used to end the run.

    ``error`` in the returned block means "no usable image", and the caller
    must treat it as a harness failure. Everything else in the block is
    recorded in the run result so the run can be reproduced from what was
    written down.
    """
    _run = _subprocess_run if _subprocess_run is not None else subprocess.run
    inspect = _inspect if _inspect is not None else _compute_inspect_local_image
    login = _login if _login is not None else _compute_docker_login

    block: dict = {
        "reference": image,
        "pull_policy": policy,
        # The cloud payload's ``container_image_digest_pinned`` is a claim
        # about the REFERENCE. It fails open to a mutable tag, which is why the
        # realised digest below is recorded regardless of what it says.
        "digest_pinned_reference": "@sha256:" in image,
    }
    local = inspect(image, _subprocess_run=_run)

    if policy == "never":
        if local is None:
            block["error"] = (
                f"image {image!r} is not in this host's docker image store and "
                f"--pull never forbids fetching it"
            )
            return block
        block["source"] = "local"
    elif policy == "always" or local is None:
        if registry_auth and registry_auth.get("password"):
            if not login(registry_auth):
                block["error"] = f"docker login failed for image {image!r}"
                return block
        if not quiet:
            print(f"docker pull : {image}")
        pull_rc = 1
        try:
            pull = _run(
                ["docker", "pull", image],
                stdout=subprocess.DEVNULL if quiet else None,
            )
            pull_rc = getattr(pull, "returncode", 1)
        except Exception as exc:  # noqa: BLE001 -- docker absent/unreachable
            block["pull_error"] = str(exc)
        if pull_rc != 0:
            if local is None:
                block["error"] = (
                    f"docker pull {image!r} failed (exit {pull_rc}) and no local "
                    f"copy of the image exists to run instead"
                )
                return block
            # A locally BUILT image is in no registry, so this pull failure is
            # expected and harmless: the image that will run is already here.
            block["pull_failed_exit_code"] = pull_rc
            block["source"] = "local"
            if not quiet:
                print(
                    f"docker pull : failed (exit {pull_rc}); running the local "
                    f"image already present"
                )
        else:
            block["source"] = "pulled"
            local = inspect(image, _subprocess_run=_run)
    else:
        block["source"] = "local"
        if not quiet:
            print("docker pull : skipped (image already present on this host)")

    identity = local or {}
    block["image_id"] = identity.get("id")
    block["repo_digests"] = identity.get("repo_digests") or []
    block["digest"] = _compute_realised_image_digest(image, identity) if identity else None
    digest = block.get("digest") or ""
    block["reproduce_reference"] = digest if "@sha256:" in digest else (
        block.get("image_id") or image
    )
    return block


def _compute_git_repo_state(start_dir: str, *, _subprocess_run=None) -> dict:
    """Describe the working tree a local ``repo_clone`` actually mounts.

    Asks git whether it can read a repository rather than looking for a
    ``.git`` DIRECTORY: in a linked worktree ``.git`` is a FILE, so an isdir
    probe reports "not a repo" and skips silently.

    That closes the LAYOUT half of "the probe cannot look"; ``-c
    safe.directory`` below closes the OWNERSHIP half. Both failures land in the
    same place -- ``resolved_by: "cwd"`` with a null commit/ref/dirty -- and
    that record is what :func:`_compute_repo_clone_warnings` exists to keep
    honest, so a silently empty one is worse than a loud refusal.
    """
    _run = _subprocess_run if _subprocess_run is not None else subprocess.run

    # Every call here is a local plumbing read (rev-parse / status) against a
    # tree already on disk. 5s is ample, and it keeps this well inside the
    # per-file budget the scripts lane grants the test that drives it -- a 20s
    # step would be most of that budget for a question git answers in
    # milliseconds.
    _GIT_READ_TIMEOUT = 5

    def _git(*a) -> "str | None":
        try:
            # ``-c safe.directory=<start_dir>``: git refuses EVERY command with
            # "detected dubious ownership" (exit 128) when the caller's uid does
            # not own the checkout -- a repo mounted into a container that runs
            # as root, or a checkout read by a service unit running as another
            # user. Every read below then returns None and the provenance record
            # degrades to ``resolved_by: "cwd"``, which reads exactly like "this
            # directory is not a checkout" instead of "git was not allowed to
            # look". Naming the path as safe makes the read WORK; it does not
            # soften what happens when it genuinely fails.
            proc = _run(
                ["git", "-c", f"safe.directory={start_dir}", "-C", start_dir, *a],
                capture_output=True, text=True, timeout=_GIT_READ_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 -- no git is "unknown", not a crash
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        return (proc.stdout or "").strip()

    root = _git("rev-parse", "--show-toplevel")
    state: dict = {
        "root": root or os.path.abspath(start_dir),
        "resolved_by": "git" if root else "cwd",
        "commit": None,
        "ref": None,
        "dirty": None,
    }
    if root:
        state["commit"] = _git("rev-parse", "HEAD")
        state["ref"] = _git("rev-parse", "--abbrev-ref", "HEAD")
        status = _git("status", "--porcelain")
        state["dirty"] = bool(status) if status is not None else None
    return state


def _compute_repo_clone_warnings(declared: dict, state: dict) -> "list[str]":
    """Name every way the mounted tree differs from what the spec declared.

    The local path MOUNTS this checkout; it does not clone ``owner/repo@ref``
    the way the cloud backend and the runner do. Saying so is the difference
    between a reproducible record and a confident lie about which ref ran.
    """
    warnings: list[str] = []
    commit = state.get("commit")
    ref = state.get("ref")
    declared_ref = str(declared.get("ref") or "").strip() or None
    owner = str(declared.get("owner") or "").strip()
    repo = str(declared.get("repo") or "").strip()
    if owner or repo:
        warnings.append(
            f"warning: repo_clone names {owner or '?'}/{repo or '?'}, but a local "
            f"run MOUNTS this checkout ({state.get('root')}) instead of cloning"
        )
    if (
        declared_ref
        and commit
        and declared_ref not in (ref, commit)
        and not commit.startswith(declared_ref)
    ):
        warnings.append(
            f"warning: repo_clone declares ref {declared_ref!r} but the mounted "
            f"tree is at {ref or '?'} ({(commit or '')[:12]})"
        )
    if state.get("dirty"):
        warnings.append(
            "warning: the mounted working tree has uncommitted changes, so the "
            "recorded commit does not fully describe what ran"
        )
    if state.get("resolved_by") != "git":
        warnings.append(
            f"warning: {state.get('root')} is not a readable git repository; "
            f"no commit was recorded for this run"
        )
    return warnings


def _compute_read_python_packages(artifact_dir: str) -> "list[str] | None":
    """Read the freeze the experiment shell wrote, in the environment that ran."""
    path = os.path.join(artifact_dir, _COMPUTE_PACKAGES_FILENAME)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return None
    return lines[:_COMPUTE_MAX_RECORDED_PACKAGES] or None


def _compute_write_environment(artifact_dir: str, environment: dict) -> "str | None":
    """Persist the realised environment next to the artifacts. Never fatal."""
    path = os.path.join(artifact_dir, _COMPUTE_ENVIRONMENT_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(environment, fh, indent=2, sort_keys=True, default=str)
    except OSError:
        return None
    return path


def _compute_environment_block(
    *,
    shell_plan: "_ShellPlan",
    gpu_info: dict,
    image_block: "dict | None",
    repo_state: "dict | None",
    setup_step: "str | None",
    spec_env: "dict[str, str] | None",
    python_packages: "list[str] | None" = None,
    python_packages_by_arm: "dict | None" = None,
) -> dict:
    """The REALISED environment: what actually ran, not what was requested.

    ``container_image_digest_pinned`` in the cloud payload is a label on the
    REFERENCE and fails open to a mutable tag whose layers can change between
    runs. This block instead records the digest docker resolved, the commit of
    the tree that was mounted, and the package set frozen inside the
    environment AFTER setup_step -- enough to re-create the run from what was
    written down.
    """
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "shell": shell_plan.label,
        },
        "gpu": gpu_info,
        "container_image": image_block,
        "repo": repo_state,
        "setup_step": setup_step,
        # KEY NAMES ONLY. An env override routinely carries a token, and this
        # block is written to disk and printed; the names are enough to explain
        # the run without turning the record into a credential leak.
        "env_override_keys": sorted(spec_env or {}),
        "python_packages": python_packages,
        "python_packages_by_arm": python_packages_by_arm,
    }


def _render_realised_environment(result: dict) -> None:
    """Human render of the recorded environment, shared by both run paths."""
    environment = result.get("environment") or {}
    image = environment.get("container_image") or {}
    if image.get("digest"):
        print(f"image sha   : {image['digest']}")
    repo = environment.get("repo") or {}
    if repo.get("commit"):
        dirty = "  (dirty working tree)" if repo.get("dirty") else ""
        print(f"repo commit : {repo['commit'][:12]}{dirty}")
    packages = environment.get("python_packages")
    if packages:
        print(f"packages    : {len(packages)} python packages recorded")
    by_arm = environment.get("python_packages_by_arm") or {}
    captured_arms = [name for name, pkgs in by_arm.items() if pkgs]
    if captured_arms:
        print(f"packages    : recorded for {len(captured_arms)} arm(s)")
    if result.get("environment_file"):
        print(f"environment : {result['environment_file']}")


def _compute_run_local(args: argparse.Namespace) -> int:
    """Execute a compute_experiment command locally without the orchestrator.

    Sets BLUEPRINT_RESULT_FILE and BLUEPRINT_ARTIFACT_DIR so the command can
    emit metrics and artifacts via the standard file-contract, then reports the
    result inline.

    Returns the documented `compute run` exit code (see _COMPUTE_EXIT_*). A run
    that could not measure what it declared is never SUCCEEDED and never 0.
    """
    declared_metric: str | None = None
    success_criterion: dict | None = None
    arms: list | None = None
    container_image: str | None = None
    container_registry_auth: dict | None = None
    spec_env: dict[str, str] | None = None
    spec_cwd: str | None = None
    spec_repo_clone: dict | None = None
    setup_step: str | None = None
    # Every spec field the local path does not act on is reported here and
    # echoed into the result. A silently dropped field is how a run ends up
    # measuring something other than what its author declared.
    spec_warnings: list[str] = []

    def _warn(message: str) -> None:
        spec_warnings.append(message)
        print(message, file=sys.stderr)

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
            sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)
        if not isinstance(spec, dict):
            print("error: spec JSON must be a JSON object")
            sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)
        fatal_fields, ignored_fields = _compute_spec_field_report(spec)
        for key in fatal_fields:
            print(
                f"error: spec field {key!r} {_COMPUTE_LOCAL_SPEC_UNSUPPORTED[key]}",
                file=sys.stderr,
            )
        if fatal_fields:
            sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)
        for key in ignored_fields:
            _warn(_compute_spec_ignored_warning(key))
        arms = spec.get("arms") or None
        command = spec.get("command")
        if not command and not arms:
            print("error: spec JSON must contain a 'command' field (or an 'arms' list)")
            sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)
        declared_metric = spec.get("declared_metric")
        raw_sc = spec.get("success_criterion")
        if isinstance(raw_sc, dict):
            success_criterion = raw_sc
        elif raw_sc is not None:
            print("error: spec 'success_criterion' must be an object with metric/operator/threshold")
            sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)
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
        raw_setup = spec.get("setup_step")
        if isinstance(raw_setup, str) and raw_setup.strip():
            setup_step = raw_setup
        elif raw_setup is not None:
            print("error: spec 'setup_step' must be a non-empty shell command string")
            sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)
        for i, raw_arm in enumerate(arms or []):
            if not isinstance(raw_arm, dict):
                continue
            for key in sorted(set(raw_arm) - {"name", "command", "setup_step"}):
                _warn(
                    f"warning: arm {raw_arm.get('name', i)!r} field {key!r} is not "
                    f"read by `blueprint compute run` and was ignored"
                )
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
        sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)

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

    # Resolve the experiment shell ONCE, explicitly. Never launch the bare name
    # "bash" on Windows — see _compute_resolve_shell for why that silently lands
    # in WSL with an empty environment block.
    shell_plan = _compute_resolve_shell()

    image_block: dict | None = None
    if container_image:
        quiet = _json_mode(args)
        if not quiet:
            print(f"image       : {container_image}")
        image_block = _compute_resolve_image(
            container_image,
            policy=(getattr(args, "pull_policy", None) or "missing"),
            registry_auth=container_registry_auth,
            quiet=quiet,
        )
        if image_block.get("error"):
            print(f"error: {image_block['error']}", file=sys.stderr)
            sys.exit(_COMPUTE_EXIT_HARNESS_FAILED)
        if not quiet:
            print(
                f"digest      : {image_block.get('digest') or 'unknown'}  "
                f"(source: {image_block.get('source')})"
            )

    # A local run MOUNTS this checkout for repo_clone rather than cloning
    # owner/repo@ref. Resolve the real repository root -- not whatever the
    # current directory happens to be -- and record which commit it was at.
    repo_state: dict | None = None
    repo_mount_root: str | None = None
    if spec_repo_clone is not None:
        repo_state = _compute_git_repo_state(os.path.abspath("."))
        repo_mount_root = repo_state["root"]
        for message in _compute_repo_clone_warnings(spec_repo_clone, repo_state):
            _warn(message)
    if setup_step and not _json_mode(args):
        print(f"setup_step  : {setup_step}")

    if arms:
        arm_names = [a.get("name", f"arm_{i}") for i, a in enumerate(arms)]
        if not _json_mode(args):
            print(f"arms        : {arm_names}")
            print(f"artifacts   : {artifact_dir}")
            if setup_step:
                print(f"setup_step  : {setup_step}")
            if gpu_info["detected"]:
                print(f"gpu         : {env['BLUEPRINT_LOCAL_GPU']}")
            else:
                print("gpu         : none")
            if declared_metric:
                print(f"declared    : {declared_metric}")
            print("running arms...")

        arm_results: dict[str, dict] = {}
        arm_metrics_by_name: dict[str, dict | None] = {}
        arm_python_packages: dict[str, list[str] | None] = {}
        overall_exit_code = 0
        any_arm_failed = False
        for i, arm in enumerate(arms):
            arm_name = arm.get("name", f"arm_{i}")
            arm_slug = _compute_arm_slug(arm_name)
            arm_result_file = os.path.join(tmpdir, f"result_{arm_slug}.json")
            arm_stderr_file = os.path.join(tmpdir, f"stderr_{arm_slug}.txt")
            # Per-arm artifact directory: sharing one directory across arms let
            # two arms writing the same filename silently clobber each other —
            # the identical defect already fixed for per-arm result files.
            arm_artifact_dir = os.path.join(artifact_dir, arm_slug)
            os.makedirs(arm_artifact_dir, exist_ok=True)
            # setup_step runs in the SAME shell and the SAME boundary as the
            # command; the spec-level step runs before any arm-level one.
            arm_cmd = _compute_fold_setup_step(
                _compute_fold_setup_step(
                    arm.get("command", ""), arm.get("setup_step")
                ),
                setup_step,
            )
            arm_env = env.copy()
            arm_env["BLUEPRINT_RESULT_FILE"] = arm_result_file
            arm_env["BLUEPRINT_ARTIFACT_DIR"] = arm_artifact_dir

            if not _json_mode(args):
                print(f"  [{arm_name}] running...")
            with open(arm_stderr_file, "wb") as arm_stderr_fh:
                if container_image:
                    container_result = f"/tmp/blueprint_out/result_{arm_slug}.json"
                    container_artifacts = f"/tmp/blueprint_out/artifacts/{arm_slug}"
                    docker_arm_cmd = ["docker", "run", "--rm"]
                    if gpu_info["detected"]:
                        docker_arm_cmd += ["--gpus", "all"]
                    if spec_repo_clone:
                        _repo_root = repo_mount_root or os.path.abspath(".")
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
                        "-e", f"BLUEPRINT_ARTIFACT_DIR={container_artifacts}",
                        container_image, "bash", "-lc",
                        _compute_shell_command(
                            _ShellPlan("bash", _COMPUTE_SHELL_POSIX, "container bash"),
                            arm_cmd,
                            {},
                        ),
                    ]
                    arm_proc = subprocess.run(docker_arm_cmd, stderr=arm_stderr_fh)
                else:
                    arm_env.update(spec_env or {})
                    arm_contract = {
                        "BLUEPRINT_RESULT_FILE": arm_result_file,
                        "BLUEPRINT_ARTIFACT_DIR": arm_artifact_dir,
                        "BLUEPRINT_LOCAL_GPU": arm_env["BLUEPRINT_LOCAL_GPU"],
                        **(spec_env or {}),
                    }
                    arm_proc = subprocess.run(
                        _compute_shell_invocation(
                            shell_plan, arm_cmd, arm_contract,
                            script_path=os.path.join(tmpdir, f"run_{arm_slug}.sh"),
                        ),
                        env=arm_env, cwd=spec_cwd or None,
                        stderr=arm_stderr_fh,
                    )
            arm_exit_code = arm_proc.returncode
            # Each arm may install its own dependencies, so the freeze is
            # per-arm: one shared answer would describe at most one of them.
            arm_python_packages[arm_name] = _compute_read_python_packages(
                arm_artifact_dir
            )
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
            arm_metrics_by_name[arm_name] = arm_metrics

            metric_missing = (
                declared_metric is not None
                and not result_missing
                and arm_metrics is not None
                and arm_metrics.get(declared_metric) is None
            )

            # Build canonical per-arm entry
            is_failure = arm_exit_code != 0 or result_missing or metric_missing
            if is_failure:
                any_arm_failed = True
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

        # A sweep whose arms exited 0 but measured nothing is not a success —
        # that was the same silent-green defect the single-command path had.
        status = "SUCCEEDED" if overall_exit_code == 0 and not any_arm_failed else "FAILED"
        metrics: dict | None = {"arms": arm_results}
        # The arms path used to hardcode verdict=None, so a multi-arm sweep —
        # the exact shape a real hypothesis test uses — could never produce a
        # verdict locally. Reuse the single-command evaluator, per arm.
        verdict: dict | None = None
        if success_criterion is not None:
            verdict = _evaluate_criterion_across_arms(arm_metrics_by_name, success_criterion)
        environment = _compute_environment_block(
            shell_plan=shell_plan,
            gpu_info=gpu_info,
            image_block=image_block,
            repo_state=repo_state,
            setup_step=setup_step,
            spec_env=spec_env,
            python_packages_by_arm=arm_python_packages,
        )
        result = {"status": status, "exit_code": overall_exit_code, "metrics": metrics,
                  "declared_metric": declared_metric, "verdict": verdict, "gpu": gpu_info,
                  "artifact_dir": artifact_dir,
                  "environment": environment,
                  "environment_file": _compute_write_environment(artifact_dir, environment),
                  "spec_warnings": spec_warnings}

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
            _render_criterion_verdict(r.get("verdict"))
            _render_realised_environment(r)

        _emit(args, result, _render)
        return _compute_run_exit_code(
            exit_code=overall_exit_code,
            measurement_failed=any_arm_failed,
            verdict=verdict,
        )

    result_file = os.path.join(tmpdir, "result.json")
    env["BLUEPRINT_RESULT_FILE"] = result_file

    stderr_file = os.path.join(tmpdir, "stderr.txt")

    effective_command = _compute_fold_setup_step(command, setup_step)

    if not _json_mode(args):
        print(f"command     : {command}")
        print(f"result_file : {result_file}")
        print(f"artifacts   : {artifact_dir}")
        print(f"shell       : {shell_plan.label}")
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
            _repo_root = repo_mount_root or os.path.abspath(".")
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
            container_image, "bash", "-lc",
            _compute_shell_command(
                _ShellPlan("bash", _COMPUTE_SHELL_POSIX, "container bash"),
                effective_command,
                {},
            ),
        ]
        proc = _run_capturing_stderr(docker_single_cmd, stderr_path=stderr_file)
        result_file = os.path.join(tmpdir, "result.json")
    else:
        env.update(spec_env or {})
        contract = {
            "BLUEPRINT_RESULT_FILE": result_file,
            "BLUEPRINT_ARTIFACT_DIR": artifact_dir,
            "BLUEPRINT_LOCAL_GPU": env["BLUEPRINT_LOCAL_GPU"],
            **(spec_env or {}),
        }
        proc = _run_capturing_stderr(
            _compute_shell_invocation(
                shell_plan, effective_command, contract,
                script_path=os.path.join(tmpdir, "run.sh"),
            ),
            env=env, cwd=spec_cwd or None, stderr_path=stderr_file,
        )
    exit_code = proc.returncode

    metrics = None
    result_missing = not os.path.exists(result_file)
    if not result_missing:
        try:
            with open(result_file) as f:
                metrics = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"  warning: could not parse result file: {exc}")
            result_missing = True
    if metrics is not None and not isinstance(metrics, dict):
        # A result file that is not a flat JSON object cannot carry a metric.
        metrics = None
        result_missing = True

    verdict = None
    if success_criterion is not None:
        verdict = _evaluate_criterion(metrics, success_criterion)

    metric_missing = (
        declared_metric is not None
        and (metrics is None or _find_metric_case_insensitive(metrics, declared_metric) is None)
    )
    # THE INVARIANT: a run that declared something to measure and did not
    # capture it is a FAILURE with a reason, on every platform. Reporting
    # SUCCEEDED here is what let a Windows/WSL run that structurally could not
    # write its result file be announced as a success with "(none emitted)".
    measurement_expected = declared_metric is not None or success_criterion is not None
    measurement_failed = measurement_expected and (
        result_missing
        or metric_missing
        or (verdict is not None and verdict.get("verdict") == "NOT EVALUABLE")
    )
    status = "SUCCEEDED" if exit_code == 0 and not measurement_failed else "FAILED"

    environment = _compute_environment_block(
        shell_plan=shell_plan,
        gpu_info=gpu_info,
        image_block=image_block,
        repo_state=repo_state,
        setup_step=setup_step,
        spec_env=spec_env,
        python_packages=_compute_read_python_packages(artifact_dir),
    )
    result = {"status": status, "exit_code": exit_code, "metrics": metrics,
              "declared_metric": declared_metric, "verdict": verdict, "gpu": gpu_info,
              "artifact_dir": artifact_dir,
              "environment": environment,
              "environment_file": _compute_write_environment(artifact_dir, environment),
              "spec_warnings": spec_warnings}
    if status == "FAILED":
        # Match the arms path's diagnostics: a classified reason plus the tail
        # of stderr, instead of a bare "FAILED, exit_code=1".
        result["error"] = _compute_arm_failure_reason(
            exit_code,
            declared_metric,
            result_missing=result_missing and measurement_expected,
            metric_missing=metric_missing,
            subject="command",
        )
        excerpt = _read_arm_stderr_excerpt(stderr_file)
        if excerpt:
            result["stderr_excerpt"] = excerpt

    def _render(r: dict) -> None:
        print(f"status      : {r['status']} (exit_code={r['exit_code']})")
        m = r.get("metrics")
        dm = r.get("declared_metric")
        if m:
            print(f"metrics     : {m}")
            if dm:
                val = _find_metric_case_insensitive(m, dm)
                if val is not None:
                    print(f"  {dm} (declared) : {val}")
                else:
                    print(f"  {dm} (declared) : NOT CAPTURED")
        else:
            print("metrics     : (none emitted)")
        err = r.get("error")
        if err:
            print(f"error       : {err}")
            excerpt = r.get("stderr_excerpt")
            if excerpt:
                print(f"stderr      : {excerpt[:400]}")
        _render_criterion_verdict(r.get("verdict"))
        _render_realised_environment(r)

    _emit(args, result, _render)
    return _compute_run_exit_code(
        exit_code=exit_code,
        measurement_failed=measurement_failed,
        verdict=verdict,
    )


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

def _message_offset(value: str) -> int:
    """Parse a bounded message-list offset without a huge choices collection."""
    try:
        offset = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("offset must be an integer") from exc
    if not 0 <= offset <= 1_000_000:
        raise argparse.ArgumentTypeError("offset must be between 0 and 1000000")
    return offset


def _repair_timestamp(value: str) -> str:
    """Require an ISO-8601 repair fence with an explicit UTC offset."""

    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "timestamp must be ISO-8601 (for example 2026-08-01T00:00:00Z)"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return value


def _repair_safe_identifier(value: str) -> str:
    """Match the endpoint's bounded revision/operation identifier alphabet."""

    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", normalized):
        raise argparse.ArgumentTypeError(
            "value must be 1..128 safe identifier characters"
        )
    return normalized


def _repair_candidate_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("candidate count must be an integer") from exc
    if not 1 <= count <= 250:
        raise argparse.ArgumentTypeError("candidate count must be between 1 and 250")
    return count


def _repair_candidate_digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError(
            "candidate digest must be exactly 64 lowercase hex characters"
        )
    return value


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
                             "(resolved through the selected credential's "
                             "authorized project catalog).")

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
    p_projects = sub.add_parser(
        "projects", parents=[common],
        help="List all projects, or onboard a new one from a GitHub repo.",
        description="Project inventory and onboarding. Defaults to `list` when no "
                    "subcommand is given.")
    projects_sub = p_projects.add_subparsers(dest="projects_cmd")
    projects_sub.add_parser("list", parents=[common],
                            help="List all projects (id, name, status).")
    p_projects_onboard = projects_sub.add_parser(
        "onboard", parents=[common],
        help="Create a project from a GitHub repo (confirms before creating).",
        description="POST /api/admin/projects/onboard. Detects the repo build "
                    "config, creates the project, seeds the goal into strategic "
                    "intent, and hands it to you with autonomy OFF. Use --preview "
                    "to see the detected build config without creating anything.")
    p_projects_onboard.add_argument("--owner", required=True, metavar="OWNER",
                                    help="GitHub owner/org of the repository.")
    p_projects_onboard.add_argument("--repo", required=True, metavar="REPO",
                                    help="GitHub repository name.")
    p_projects_onboard.add_argument("--name", default=None, metavar="NAME",
                                    help="Project display name (default: owner/repo).")
    onboard_goal = p_projects_onboard.add_mutually_exclusive_group()
    onboard_goal.add_argument(
        "--goal", default=None, metavar="TEXT",
        help=f"Goal seeded into strategic intent (max {_ONBOARD_GOAL_MAX_CHARS} chars).")
    onboard_goal.add_argument(
        "--goal-file", dest="goal_file", default=None, metavar="PATH",
        help="Read the goal from a UTF-8 file — use this for multi-paragraph "
             "goals instead of shell-quoting them.")
    p_projects_onboard.add_argument(
        "--promotion-target", dest="promotion_target", default=None, metavar="REF",
        help="Promotion target branch (default: the repo default branch).")
    p_projects_onboard.add_argument(
        "--preview", action="store_true",
        help="Detect and print the build config without creating a project "
             "(preview_only=true; mutates nothing, never confirms).")
    p_projects_onboard.add_argument("--yes", action="store_true",
                                    help="Skip the confirmation prompt.")

    # coding-agent access bootstrap and CLI fallback
    p_agent_access = sub.add_parser(
        "agent-access",
        parents=[common],
        help="Grant, inspect, and verify scoped service access.",
    )
    agent_access_sub = p_agent_access.add_subparsers(dest="agent_access_cmd")
    agent_access_sub.add_parser(
        "whoami", parents=[common], help="Show effective credential grants and capabilities."
    )
    agent_access_sub.add_parser(
        "snapshot", parents=[common], help="Show the canonical project orientation snapshot."
    )
    p_agent_access_grant = agent_access_sub.add_parser(
        "grant", parents=[common], help="Mint one scoped service credential."
    )
    p_agent_access_grant.add_argument("name", help="Unique lowercase credential name.")
    p_agent_access_grant.add_argument(
        "--description", default=None, help="Optional credential purpose/owner note."
    )
    p_agent_access_grant.add_argument(
        "--profile",
        choices=["coding-agent-read", "project-operator"],
        default="coding-agent-read",
        help=(
            "Access profile: read-only coding context (viewer) or the narrow "
            "project-operator CLI surface (member)."
        ),
    )
    p_agent_access_grant.add_argument(
        "--expires-in-days",
        type=int,
        default=30,
        choices=range(1, 366),
        metavar="1..365",
        help="Credential lifetime (default: 30 days).",
    )
    p_agent_access_grant.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    agent_access_sub.add_parser(
        "list", parents=[common], help="List service credentials (admin only)."
    )
    p_agent_access_revoke = agent_access_sub.add_parser(
        "revoke", parents=[common], help="Revoke a service credential (admin only)."
    )
    p_agent_access_revoke.add_argument("credential_id", help="Credential UUID.")
    p_agent_access_revoke.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )

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
    p_ops_claim_repair = ops_sub.add_parser(
        "repair-fast-claim-redactions",
        parents=[common],
        help="Review or apply the exact false Fast-claim redaction repair set.",
        description=(
            "Owner-only two-phase repair. Run dry-run first, then copy its exact "
            "candidate count and digest into apply."
        ),
    )
    claim_repair_sub = p_ops_claim_repair.add_subparsers(
        dest="fast_claim_repair_mode",
        required=True,
    )

    def add_claim_repair_fences(mode_parser: argparse.ArgumentParser) -> None:
        mode_parser.add_argument(
            "--window-start",
            required=True,
            type=_repair_timestamp,
            help="Inclusive ISO-8601 start timestamp with timezone.",
        )
        mode_parser.add_argument(
            "--window-end",
            required=True,
            type=_repair_timestamp,
            help="Exclusive ISO-8601 end timestamp with timezone.",
        )
        mode_parser.add_argument(
            "--buggy-serving-revision",
            required=True,
            type=_repair_safe_identifier,
            help="Exact revision that produced the false redactions.",
        )
        mode_parser.add_argument(
            "--expected-serving-revision",
            required=True,
            type=_repair_safe_identifier,
            help="Exact fixed revision currently expected to be serving.",
        )

    p_ops_claim_dry_run = claim_repair_sub.add_parser(
        "dry-run",
        parents=[common],
        help="Return only the bounded candidate count and digest; mutate nothing.",
    )
    add_claim_repair_fences(p_ops_claim_dry_run)
    p_ops_claim_apply = claim_repair_sub.add_parser(
        "apply",
        parents=[common],
        help="Apply one previously reviewed and revision-fenced repair set.",
    )
    add_claim_repair_fences(p_ops_claim_apply)
    p_ops_claim_apply.add_argument(
        "--operation-id",
        required=True,
        type=_repair_safe_identifier,
        help="Immutable idempotency id for this reviewed repair operation.",
    )
    p_ops_claim_apply.add_argument(
        "--expected-candidate-count",
        required=True,
        type=_repair_candidate_count,
        help="Exact non-zero candidate count returned by dry-run (1..250).",
    )
    p_ops_claim_apply.add_argument(
        "--expected-candidate-digest",
        required=True,
        type=_repair_candidate_digest,
        help="Exact lowercase SHA-256 candidate digest returned by dry-run.",
    )
    p_ops_claim_apply.add_argument(
        "--yes",
        required=True,
        action="store_true",
        help="Required explicit acknowledgement that the reviewed repair may mutate.",
    )
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
    p_tasks_list.add_argument(
        "--status", action="append", default=[], metavar="STATUS",
        help="Filter by task status (repeatable; e.g. --status FAILED --status BLOCKED).")
    p_tasks_list.add_argument(
        "--active", action="store_true",
        help="Include QUEUED, PROVISIONING, IN_PROGRESS, and EVALUATING tasks.")
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
    p_tasks_cancel.add_argument(
        "--reason", default=None, help="Optional bounded cancellation reason."
    )
    p_tasks_cancel.add_argument(
        "--confirm-cascade",
        action="store_true",
        help="Acknowledge cancellation of reported child/dependent tasks.",
    )
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
    p_tasks_request_changes = tasks_sub.add_parser(
        "request-changes",
        parents=[common],
        help="Request a bounded fix or review follow-up for an approval-gated task.",
    )
    p_tasks_request_changes.add_argument("task_id", help="Task id (full uuid).")
    p_tasks_request_changes.add_argument(
        "feedback", help="Concrete feedback delivered to the follow-up task."
    )
    p_tasks_request_changes.add_argument(
        "--action", choices=["fix", "review"], default="fix"
    )
    p_tasks_request_changes.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
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
        help="Task type from Blueprint's server capability registry (default: create).")
    p_tasks_create.add_argument(
        "--repo", default=None, metavar="OWNER/REPO",
        help="GitHub repo (default: the resolved project's configured repo).")
    p_tasks_create.add_argument(
        "--base-branch", dest="base_branch", default=None,
        help="Base branch override (default: the resolved project's promotion target).")
    p_tasks_create.add_argument(
        "--model", default=None, help="Model override for this task.")
    p_tasks_create.add_argument(
        "--effort", default=None,
        choices=["low", "medium", "high", "max"],
        help="Effort/reasoning level override.")
    p_tasks_create.add_argument(
        "--hotfix", action="store_true", help="Mark task as hotfix priority.")
    p_tasks_create.add_argument("--pr-number", type=int, default=None,
                                help="Pull request number (required for review/fix).")
    p_tasks_create.add_argument("--branch", default=None,
                                help="Pull request branch (required for review/fix).")
    p_tasks_create.add_argument("--idempotency-key", default=None,
                                help="Stable retry-deduplication key.")
    p_tasks_create.add_argument("--require-deps-landed-on", default=None,
                                help="Hold until dependencies land on this branch.")
    p_tasks_create.add_argument("--release-base-branch", default=None,
                                help="Base branch to use after dependency release.")
    p_tasks_create.add_argument(
        "--acceptance-test", default=None, metavar="PATH[::TEST]",
        help="Declare a named test as acceptance evidence.")
    p_tasks_create.add_argument(
        "--acceptance-behavior", default=None, metavar="DESCRIPTION",
        help="Declare an observable behavior as acceptance evidence.")
    p_tasks_create.add_argument(
        "--acceptance-evidence-file", default=None, metavar="JSON_FILE",
        help="Load an advanced acceptance-evidence JSON object from a file.")
    p_tasks_create.add_argument(
        "--work-class", choices=["exploratory", "confirmatory", "productization"],
        default=None, help="Research-only empirical work class.")
    p_tasks_create.add_argument(
        "--stop-or-pivot-condition", default=None,
        help="Research-only condition for stopping or pivoting.")
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
    p_sys_autonomy = system_sub.add_parser(
        "autonomy", parents=[common],
        help="Show autonomous research state per project, or toggle it "
             "for the selected project with --enable/--disable.")
    p_sys_autonomy.add_argument("--enable", action="store_true",
                                help="Set autonomous_enabled=true on the selected project (confirms).")
    p_sys_autonomy.add_argument("--disable", action="store_true",
                                help="Set autonomous_enabled=false on the selected project (confirms).")
    p_sys_autonomy.add_argument("--yes", action="store_true", help="Skip confirmation.")

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
    p_dirs_list.add_argument(
        "--limit", type=int, choices=range(1, 101), default=50,
        metavar="1..100", help="Scoped direction page size (default 50).")
    p_dirs_list.add_argument(
        "--offset", type=_message_offset, default=0,
        metavar="0..1000000", help="Scoped direction rows to skip.")

    p_dirs_show = dirs_sub.add_parser("show", parents=[common],
                                      help="Show one direction in detail.")
    p_dirs_show.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")

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
    p_dirs_add.add_argument(
        "--idempotency-key", default=None,
        help="Optional stable retry key for exactly-once direction creation.")
    p_dirs_add.add_argument("--yes", action="store_true",
                            help="Skip the confirmation prompt.")

    p_dirs_update = dirs_sub.add_parser("update", parents=[common],
                                        help="Update a direction (confirms before patching).")
    p_dirs_update.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")
    p_dirs_update.add_argument("--text", help="New direction text.")
    p_dirs_update.add_argument("--priority", choices=["high", "medium", "low"],
                               help="New priority.")
    p_dirs_update.add_argument(
        "--paths", nargs="*", metavar="PATH",
        help="Replace associated paths; pass the flag with no paths to clear them.")
    p_dirs_update.add_argument(
        "--completion-criteria", dest="completion_criteria", metavar="TEXT",
        help="Replace completion criteria text.")
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
    p_dirs_unhold.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")
    p_dirs_unhold.add_argument("--yes", action="store_true",
                               help="Skip the confirmation prompt.")

    p_dirs_resolve = dirs_sub.add_parser(
        "resolve-discovery", parents=[common],
        help="Resolve an ask_operator discovery cycle: continue with a "
             "bounded next bet, park, or close (confirms before posting).")
    p_dirs_resolve.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")
    p_dirs_resolve.add_argument("--cycle-id", required=True,
                                help="Discovery cycle UUID (from tasks autonomy-debug).")
    p_dirs_resolve.add_argument("--cycle-revision", type=int, required=True,
                                help="Current cycle revision (optimistic-lock token).")
    p_dirs_resolve.add_argument("--action", required=True,
                                choices=["continue", "park", "close"],
                                help="Operator disposition for the cycle.")
    p_dirs_resolve.add_argument("--bet-mode",
                                choices=["research", "measure", "build"],
                                help="continue only: next bet mode.")
    p_dirs_resolve.add_argument("--bet-claim",
                                help="continue only: the claim the bet tests.")
    p_dirs_resolve.add_argument("--bet-question",
                                help="continue only: the decision question.")
    p_dirs_resolve.add_argument("--bet-success",
                                help="continue only: observable success evidence.")
    p_dirs_resolve.add_argument("--bet-retirement",
                                help="continue only: retirement condition.")
    p_dirs_resolve.add_argument("--note", help="Optional operator note (2000 chars max).")
    p_dirs_resolve.add_argument("--yes", action="store_true",
                                help="Skip the confirmation prompt.")

    p_dirs_show_discovery = dirs_sub.add_parser(
        "show-discovery", parents=[common],
        help="Show a direction's most recent discovery cycles: state, "
             "verdict, operator_question, and binding-decision authority.")
    p_dirs_show_discovery.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")
    p_dirs_show_discovery.add_argument("--limit", type=int, default=5,
                                       help="Max cycles to show, newest first "
                                            "(default 5, max 20).")

    p_dirs_retarget = dirs_sub.add_parser("retarget", parents=[common],
                                          help="Retarget a staged direction.")
    p_dirs_retarget.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")
    p_dirs_retarget.add_argument("version", type=int,
                                 help="New version to stage against.")
    p_dirs_retarget.add_argument("--yes", action="store_true",
                                 help="Skip the confirmation prompt.")

    p_dirs_activate = dirs_sub.add_parser("activate", parents=[common],
                                          help="Activate a staged direction immediately.")
    p_dirs_activate.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")
    p_dirs_activate.add_argument("--yes", action="store_true",
                                  help="Skip the confirmation prompt.")

    p_dirs_cancel = dirs_sub.add_parser("cancel", parents=[common],
                                        help="Cancel a staged direction.")
    p_dirs_cancel.add_argument("direction_id", help="Direction id (d-<hex>); a unique prefix, with or without the d-, also resolves.")
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
                      help="Reason recorded for cut / clear-deploy-failed / force-promote.")
    p_pr.add_argument("--force", action="store_true",
                      help="Force clear-deploy-failed when serving SHA is unobservable.")
    p_pr.add_argument("--candidate-sha", default=None,
                      help="force-promote only: pin the commit to promote "
                           "(default: current integration-branch HEAD).")

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
    p_msgs_list.add_argument(
        "--limit", type=int, choices=range(1, 201), default=50,
        metavar="1..200", help="Maximum routine messages to return (default: 50).",
    )
    p_msgs_list.add_argument(
        "--offset", type=_message_offset, default=0,
        metavar="0..1000000", help="Skip the first N messages for pagination.",
    )
    p_msgs_send = msgs_sub.add_parser("send", parents=[common],
                                      help="Send a message to the autonomous system.")
    p_msgs_send.add_argument("content", help="Message content.")
    p_msgs_send.add_argument(
        "--type", default="FEEDBACK",
        choices=["FEEDBACK", "NOTE", "BUG_REPORT", "QUESTION", "BLOCKER"],
        help="Message type: FEEDBACK|NOTE|BUG_REPORT|QUESTION|BLOCKER (default FEEDBACK).",
    )
    p_msgs_send.add_argument(
        "--idempotency-key", default=None,
        help="Optional stable retry key for exactly-once message creation.",
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
    fleet_sub.add_parser(
        "runners", parents=[common],
        help="List local runners with enrollment project and project bindings.",
        description="List fleet runners (hostname, status, enrollment project, and "
                    "the projects each is allowed to serve). Scoped to one project "
                    "with --project-id/--project; otherwise sweeps every authorized "
                    "project.")
    p_fleet_grant = fleet_sub.add_parser(
        "grant", parents=[common],
        help="Bind a project to a runner's allow-list (confirms before mutating).",
        description="Add a project to a runner's per-project allow-list so the fleet "
                    "can dispatch that project's tasks to it. The project is named by "
                    "the global --project-id (or --project) flag; --runner accepts a "
                    "runner id, id prefix, or hostname. Already-bound is reported and "
                    "succeeds.")
    p_fleet_grant.add_argument("--runner", required=True, metavar="ID_OR_HOSTNAME",
                               help="Runner id, id prefix, or hostname.")
    p_fleet_grant.add_argument("--yes", action="store_true",
                               help="Skip the confirmation prompt.")
    p_fleet_revoke = fleet_sub.add_parser(
        "revoke", parents=[common],
        help="Remove a project from a runner's allow-list (confirms before mutating).",
        description="Remove a project from a runner's per-project allow-list. The "
                    "project is named by the global --project-id (or --project) flag. "
                    "The runner's enrollment project is its anchor membership and "
                    "cannot be revoked.")
    p_fleet_revoke.add_argument("--runner", required=True, metavar="ID_OR_HOSTNAME",
                                help="Runner id, id prefix, or hostname.")
    p_fleet_revoke.add_argument("--yes", action="store_true",
                                help="Skip the confirmation prompt.")

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

    # deploy
    p_deploy = sub.add_parser(
        "deploy", parents=[common],
        help="Activate the newest CI-built main release on the local control plane.")
    p_deploy.add_argument(
        "--sha", help="Deploy the release built from this commit (prefix ok); "
                      "default is the newest successful main build.")
    p_deploy.add_argument(
        "--host", default="hamsterwheel",
        help="SSH host running the control plane (default: hamsterwheel).")
    p_deploy.add_argument("--dry-run", action="store_true",
                          help="Show what would be deployed without deploying.")
    p_deploy.add_argument("--yes", action="store_true",
                          help="Skip the confirmation prompt (for scripting).")
    p_deploy.add_argument(
        "--force",
        action="store_true",
        help="Deploy even when no promoted release version exists or the built "
        "revision does not contain the promoted SHA (emergency override).",
    )

    # migrate
    p_migrate = sub.add_parser(
        "migrate", parents=[common],
        help="Run Alembic against the local serving PostgreSQL in the deployed image.",
        description=(
            "Apply local serving-database migrations in a one-off container. "
            "The --local acknowledgement is required because this command never "
            "targets the cloud database."
        ),
    )
    p_migrate.add_argument(
        "--local", action="store_true", required=True,
        help="Acknowledge that the target is the local blueprint PostgreSQL.",
    )
    p_migrate.add_argument(
        "--sha", help="Migration image revision (prefix ok); defaults to the newest successful main build.",
    )
    p_migrate.add_argument(
        "--image", "--worker-image", dest="image",
        help="Full immutable worker/app image reference; overrides --sha manifest lookup.",
    )
    p_migrate.add_argument(
        "--worker-env-file",
        help="Worker role EnvironmentFile (defaults to the installed local worker role).",
    )
    p_migrate.add_argument(
        "--worker-secrets-file",
        help="Worker secrets JSON (defaults to the worker role configuration).",
    )
    p_migrate.add_argument(
        "--registry-credential-file",
        help="Worker Artifact Registry credential JSON (defaults to the worker role configuration).",
    )
    p_migrate.add_argument(
        "--network",
        help="Private Docker network (defaults to BLUEPRINT_CONTROL_PLANE_NETWORK).",
    )
    p_deploy_agent = sub.add_parser(
        "deploy-agent", parents=[common],
        help="Run the trusted local executor for approved activations.")
    p_deploy_agent.add_argument(
        "--once", action="store_true",
        help="Poll and process at most one approved activation, then exit.")
    p_deploy_agent.add_argument(
        "--poll-interval", type=int, default=30,
        help="Seconds between approval polls (default: 30).")
    p_deploy_agent.add_argument(
        "--lock-path", default="/run/blueprint-deploy-agent/deploy-agent.lock",
        help="Local serialization lock path (managed by the systemd runtime directory).")
    p_deploy_agent.add_argument(
        "--state-path", default=_DEPLOY_AGENT_STATE_PATH,
        help="Local receipt-retry state path (managed by the systemd state directory).")
    p_deploy_agent.add_argument(
        "--poll-state-path", default=_DEPLOY_AGENT_POLL_PATH,
        help="Local successful-poll marker path (managed by the systemd runtime directory).")
    p_deploy_agent.add_argument(
        "--preflight", action="store_true",
        help="Read-only installation, polling, connectivity, and activation checklist.")

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

    p_analytics_kills = analytics_sub.add_parser(
        "platform-kills", parents=[common],
        help="Platform-killed tasks per day, by class, with the unclassified share.")
    p_analytics_kills.add_argument("--days", type=int, default=7,
                                   help="Window in days (default 7).")

    # vision
    p_vision = sub.add_parser("vision", parents=[common],
                              help="Show product vision and design document.")
    p_vision.add_argument(
        "--design-doc", dest="design_doc_key", nargs="?", const="",
        help="Show design document pages; optionally pass a page KEY to show one page.",
    )

    # strategic (resident session: start / stop / chat / send)
    p_strategic = sub.add_parser(
        "strategic", parents=[common],
        help="Resident Strategic session: start, stop, chat, or send one message.",
        description="Start (or adopt) a resident strategist session on the operator's "
                    "runner, stop it, chat interactively, or send one message.")
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
        choices=["NOTE", "QUESTION", "FEEDBACK"],
        help="Message type to send (default NOTE).")
    p_strat_send = strategic_sub.add_parser(
        "send", parents=[common],
        help="Send one non-interactive Strategic continuation message.")
    p_strat_send.add_argument("content", help="Message content.")
    p_strat_send.add_argument(
        "--type", dest="type", default="NOTE",
        choices=["NOTE", "QUESTION", "FEEDBACK"],
        help="Message type to send (default NOTE).")
    p_strat_send.add_argument(
        "--idempotency-key", default=None,
        help="Optional stable retry key for exactly-once message creation.")

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
                    "can emit metrics via the standard file contract, then reports the result. "
                    "Requires no Blueprint API config, credential, or network access.",
        epilog=_COMPUTE_RUN_EXIT_CODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_compute_run.add_argument(
        "run_command", nargs="?", default=None,
        help="Bash command string to execute. "
             "Write a flat JSON object of {metric: value} to $BLUEPRINT_RESULT_FILE to report metrics.")
    p_compute_run.add_argument(
        "--local", dest="local_spec", metavar="SPEC_JSON",
        help="Path to a spec JSON file. The JSON must contain 'command' (str) or "
             "'arms' (list), and optionally 'declared_metric' (str), "
             "'success_criterion' (metric/operator/threshold), 'setup_step' (shell "
             "run before the command in the same shell), 'container_image', 'env', "
             "'cwd', and 'repo_clone' (mounts this checkout at /workspace). Any "
             "other field is reported by name, never dropped in silence. "
             "See docs/COMPUTE_RUN_LOCAL.md."
    )
    p_compute_run.add_argument(
        "--pull", dest="pull_policy", choices=_COMPUTE_PULL_POLICIES, default="missing",
        help="When to docker pull --image. 'missing' (default) runs an image that is "
             "already on this host and pulls only when there is none, which is what "
             "makes a LOCALLY BUILT image usable; 'always' refreshes a mutable tag "
             "first; 'never' fails rather than reaching the network.")
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


_DEPLOY_REPO = "HenryHamster/BlueprintAIStudio"
_DEPLOY_WORKFLOW = "Deploy Blueprint"
_DEPLOY_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEPLOY_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_MIGRATION_IMAGE_PREFIX = (
    "us-central1-docker.pkg.dev/blueprint-orchestrator/"
    "blueprint-repo/blueprint-orchestrator"
)
_MIGRATION_IMAGE_RE = re.compile(
    rf"^{re.escape(_MIGRATION_IMAGE_PREFIX)}@sha256:[0-9a-f]{{64}}$"
)
_MIGRATION_REGISTRY = "us-central1-docker.pkg.dev"
_MIGRATION_DOCKER = "/usr/bin/docker"
_MIGRATION_NETWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_MIGRATION_HARDENING_ARGS = (
    "--pull", "never", "--read-only",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
)


def _migration_secure_file(path_text: str, label: str) -> Path:
    """Resolve a worker-owned input without following a replacement symlink."""
    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular, non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"{label} must be mode 0600 or stricter")
    return path


def _migration_role_env(path: Path) -> dict[str, str]:
    """Parse the literal systemd EnvironmentFile used by the worker unit."""
    values: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid worker EnvironmentFile entry at line {line_number}")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"worker EnvironmentFile value for {key} contains control characters")
        if key in values:
            raise ValueError(f"worker EnvironmentFile repeats {key}")
        values[key] = value
    return values


def _migration_worker_values(args: argparse.Namespace) -> tuple[dict[str, str], Path, Path, str]:
    """Load the exact worker runtime inputs needed by the migration container."""
    role_path_text = getattr(args, "worker_env_file", None) or os.environ.get(
        "BLUEPRINT_WORKER_ENV_FILE", ""
    )
    if not role_path_text:
        candidates = sorted(Path("/etc/blueprint/nodes").glob("*/roles/worker/role.env"))
        if len(candidates) != 1:
            raise ValueError(
                "could not identify one installed worker role; pass --worker-env-file"
            )
        role_path = _migration_secure_file(
            str(candidates[0]), "worker EnvironmentFile"
        )
    else:
        role_path = _migration_secure_file(role_path_text, "worker EnvironmentFile")
    role = _migration_role_env(role_path)

    def configured(name: str, argument: str | None = None) -> str:
        value = argument or role.get(name) or os.environ.get(name, "")
        if not value:
            raise ValueError(f"{name} is required by the worker migration contract")
        return value

    secrets_path = _migration_secure_file(
        configured("BLUEPRINT_WORKER_SECRETS_FILE", getattr(args, "worker_secrets_file", None)),
        "worker secrets file",
    )
    credential_path = _migration_secure_file(
        configured(
            "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE",
            getattr(args, "registry_credential_file", None),
        ),
        "worker registry credential file",
    )
    network = configured("BLUEPRINT_CONTROL_PLANE_NETWORK", getattr(args, "network", None))
    if not _MIGRATION_NETWORK_RE.fullmatch(network):
        raise ValueError("worker control-plane network is not a safe Docker network name")
    try:
        secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("worker secrets file is not valid JSON") from exc
    if not isinstance(secrets, dict) or not secrets:
        raise ValueError("worker secrets file must contain a non-empty JSON object")
    # Worker-secret values are handed to the migration container through the OS
    # environment (docker `run --env NAME`, subprocess env=...), which forwards
    # bytes verbatim EXCEPT an embedded NUL (execve / os.environ cannot carry
    # one). Legitimately multi-line secrets — e.g. a PEM-encoded GITHUB_PRIVATE_KEY
    # — must therefore be accepted; only a NUL byte is impossible to pass. (The
    # line-delimited worker role.env is a different medium, validated separately
    # by _parse_worker_env, where control chars genuinely break KEY=VALUE framing.)
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or "\x00" in value
        for key, value in secrets.items()
    ):
        raise ValueError(
            "worker secrets must be non-empty string keys mapping to string "
            "values without embedded NUL bytes"
        )
    database_url = secrets.get("DATABASE_URL")
    if not database_url:
        raise ValueError("worker secrets are missing DATABASE_URL")
    secrets["DATABASE_URL"] = _migration_local_database_url(database_url)
    return secrets, credential_path, secrets_path, network


def _migration_local_database_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg"}:
        raise ValueError("worker DATABASE_URL must use PostgreSQL")
    if not parsed.username or parsed.password is None or not parsed.path.strip("/"):
        raise ValueError("worker DATABASE_URL must include user, password, and database")
    userinfo = parsed.netloc.rsplit("@", 1)[0]
    query = [
        pair for pair in parsed.query.split("&")
        if pair and pair.partition("=")[0].lower() not in {"ssl", "sslmode"}
    ]
    query.append("ssl=disable")
    return urlunsplit(
        SplitResult(
            parsed.scheme,
            f"{userinfo}@blueprint-postgres:5432",
            parsed.path,
            "&".join(query),
            parsed.fragment,
        )
    )


def _migration_credential_text(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("worker registry credential is not valid JSON") from exc
    expected_email = (
        "blueprint-local-worker-runtime@blueprint-orchestrator.iam.gserviceaccount.com"
    )
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "service_account"
        or payload.get("project_id") != "blueprint-orchestrator"
        or payload.get("client_email") != expected_email
        or not isinstance(payload.get("private_key"), str)
    ):
        raise ValueError("worker registry credential has the wrong identity")
    return path.read_text(encoding="utf-8")


@contextlib.contextmanager
def _migration_registry_config(credential_path: Path):
    """Create the same ephemeral Docker login used by local_app_release."""
    credential_text = _migration_credential_text(credential_path)
    directory = Path(tempfile.mkdtemp(prefix="blueprint-registry-auth-", dir="/run"))
    os.chmod(directory, 0o700)
    cleanup_error: str | None = None
    try:
        login = subprocess.run(
            [
                _MIGRATION_DOCKER, "--config", str(directory), "login",
                "--username", "_json_key", "--password-stdin",
                f"https://{_MIGRATION_REGISTRY}",
            ],
            input=credential_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if login.returncode != 0:
            detail = (login.stderr or login.stdout or "").strip()
            raise RuntimeError(f"registry login failed{': ' + detail if detail else ''}")
        credential_text = ""
        yield directory
    finally:
        logout = subprocess.run(
            [_MIGRATION_DOCKER, "--config", str(directory), "logout", _MIGRATION_REGISTRY],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if logout.returncode != 0:
            cleanup_error = "docker logout failed"
        try:
            for entry in directory.iterdir():
                entry.unlink()
            directory.rmdir()
        except OSError as exc:
            cleanup_error = f"registry credential cleanup failed: {exc}"
        if cleanup_error:
            raise RuntimeError(cleanup_error)


def _migration_image(args: argparse.Namespace) -> tuple[str, str | None]:
    image = getattr(args, "image", None)
    if image:
        if not _MIGRATION_IMAGE_RE.fullmatch(image):
            raise ValueError("--image must be the approved immutable Artifact Registry worker image")
        return image, None
    release = _fetch_deploy_manifest(getattr(args, "sha", None))
    return f"{_MIGRATION_IMAGE_PREFIX}@{release['digest']}", release["revision"]


def _migration_container_command(
    config: Path,
    image: str,
    network: str,
    credential_path: Path,
    environment: dict[str, str],
    action: tuple[str, ...],
) -> list[str]:
    forwarded = sorted(
        set(environment)
        | {
            "BLUEPRINT_PROCESS_ROLE",
            "BLUEPRINT_DEPLOYMENT_MODE",
            "PYTHONDONTWRITEBYTECODE",
            "HOME",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    )
    return [
        _MIGRATION_DOCKER, "--config", str(config), "run", "--rm",
        "--network", network, *_MIGRATION_HARDENING_ARGS,
        "--mount", f"type=bind,src={credential_path},dst=/var/run/blueprint/gcp.json,readonly",
        *(item for name in forwarded for item in ("--env", name)),
        image, *action,
    ]


def _run_migration_container(command: list[str], environment: dict[str, str]) -> int:
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        timeout=600, env=environment,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        print(f"ERROR: migration container exited {result.returncode}", file=sys.stderr)
    return result.returncode


def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply local migrations with the selected immutable worker image."""
    if not args.local:  # argparse requires this; retain the guard for direct callers.
        print("ERROR: migrate requires --local", file=sys.stderr)
        return 2
    try:
        image, revision = _migration_image(args)
        secrets, credential_path, _secrets_path, network = _migration_worker_values(args)
        environment = dict(secrets)
        environment.update(
            {
                "BLUEPRINT_PROCESS_ROLE": "worker",
                "BLUEPRINT_DEPLOYMENT_MODE": "local-production",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": "/tmp",
                "GOOGLE_APPLICATION_CREDENTIALS": "/var/run/blueprint/gcp.json",
            }
        )
        print("=== LOCAL DATABASE MIGRATION ===")
        print(f"  Image:     {image}")
        if revision:
            print(f"  Revision:  {revision}")
        with _migration_registry_config(credential_path) as config:
            pull = subprocess.run(
                [_MIGRATION_DOCKER, "--config", str(config), "pull", image],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=600,
            )
            if pull.stdout:
                print(pull.stdout, end="")
            if pull.stderr:
                print(pull.stderr, end="", file=sys.stderr)
            if pull.returncode != 0:
                print(f"ERROR: migration image pull exited {pull.returncode}", file=sys.stderr)
                return pull.returncode
            base = lambda action: _migration_container_command(
                config, image, network, credential_path, environment, action
            )
            print("  Before:")
            before = _run_migration_container(base(("alembic", "current")), environment)
            if before != 0:
                return before
            upgrade = _run_migration_container(
                base(("alembic", "upgrade", "head")), environment
            )
            if upgrade != 0:
                return upgrade
            print("  After:")
            return _run_migration_container(base(("alembic", "current")), environment)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: local migration failed: {exc}", file=sys.stderr)
        return 1


def _gh(*gh_args: str) -> str:
    """Run gh and return stdout, mapping failure to APIError for main().

    gh emits UTF-8 regardless of platform; without an explicit encoding,
    Windows decodes with cp1252 and a multi-byte response byte kills the
    subprocess reader thread, silently yielding stdout=None on rc=0.
    """
    try:
        proc = subprocess.run(
            ["gh", *gh_args], capture_output=True, check=False,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: gh {' '.join(gh_args[:3])}... timed out after 120s")
        raise APIError("gh command timed out")
    if proc.returncode != 0:
        print(f"ERROR: gh {' '.join(gh_args[:3])}... failed: {proc.stderr.strip()[:400]}")
        raise APIError(proc.stderr.strip()[:400])
    return proc.stdout


def _read_latest_promoted_version(args: argparse.Namespace) -> dict | None:
    """Read the current promoted version, without interpreting CLI overrides."""
    pid = get_project_id(getattr(args, "project_id", None))
    versions_data, err = _safe_read(
        f"/api/admin/projects/{pid}/release-versions?status=promoted&limit=1"
    )
    if err:
        print(f"ERROR: could not read promoted release versions: {err}")
        raise APIError("promoted version lookup failed")
    versions = (versions_data or {}).get("versions") or []
    return versions[0] if versions else None


def _validate_deploy_binding(
    revision: str, promoted: dict, *, force: bool = False
) -> None:
    """Verify that ``revision`` is the promotion or its GitHub descendant."""
    promoted_sha = (promoted.get("promoted_sha") or "").lower()
    version_number = promoted.get("version_number")
    status = "identical"
    if promoted_sha and promoted_sha != revision:
        compare = json.loads(_gh(
            "api", f"repos/{_DEPLOY_REPO}/compare/{promoted_sha}...{revision}",
        ))
        status = (compare.get("status") or "unknown")
        if status not in ("identical", "ahead"):
            if force:
                print(f"  WARNING: built revision {revision[:12]} is {status} "
                      f"the promoted SHA {promoted_sha[:12]} — binding check "
                      "bypassed (--force).")
            else:
                print(f"ERROR: built revision {revision[:12]} is {status} the "
                      f"promoted SHA {promoted_sha[:12]}; refusing to activate a "
                      "revision that does not contain the promoted commit. Use "
                      "--force to deploy anyway.")
                raise APIError("revision not descendant of promoted sha")
    _validate_main_reachability(revision, force=force)
    print(f"  Binding:   v{version_number} (promoted {promoted_sha[:12]}, "
          f"build {status})")


def _validate_main_reachability(revision: str, *, force: bool = False) -> None:
    """Refuse to activate a revision that is not reachable from main.

    Descendant-of-promoted is NOT the same thing as on-main: a PR branch tip
    that contains the promoted commit passes the binding check above while
    carrying commits main does not have. The serving orchestrator advertises
    its own SHA as the fleet auto-update version hint, and runners git-track
    main — a hint unreachable from main puts every runner into an infinite
    update-drain retry loop (fleet-wide dispatch outage, 2026-08-17: serving
    6fa5965a was a PR branch tip whose content merged to automain as a
    different SHA, so no runner could ever satisfy the hint).
    """
    compare = json.loads(_gh(
        "api", f"repos/{_DEPLOY_REPO}/compare/main...{revision}",
    ))
    status = (compare.get("status") or "unknown")
    # "identical"/"behind" mean revision is reachable from main's tip.
    if status in ("identical", "behind"):
        return
    if force:
        print(f"  WARNING: revision {revision[:12]} is {status} relative to "
              "main — it is NOT reachable from main, so runner auto-update "
              "will drain the fleet until a main-reachable revision is "
              "deployed. Bypassed (--force).")
        return
    print(f"ERROR: revision {revision[:12]} is {status} relative to main — "
          "not reachable from main. Runners git-track main and can never "
          "satisfy this version hint; deploying it wedges fleet auto-update. "
          "Merge to main first, or use --force if you accept a drained "
          "fleet until the next main-reachable deploy.")
    raise APIError("revision not reachable from main")


def _resolve_deploy_binding(args: argparse.Namespace, revision: str) -> dict | None:
    """Resolve and verify the version this legacy CLI deploy binds to.

    ``--force`` retains its emergency legacy behavior: no promotion can mean
    an unbound deploy, and a non-descendant can still be activated deliberately.
    """
    promoted = _read_latest_promoted_version(args)
    if promoted is None:
        if getattr(args, "force", False):
            print("  WARNING: no promoted release version found — deploying "
                  "unbound (--force).")
            _validate_main_reachability(revision, force=True)
            return None
        print("ERROR: no promoted release version found for this project; "
              "refusing to activate a revision that is not bound to a "
              "promotion. Use --force to deploy anyway.")
        raise APIError("no promoted release version")
    _validate_deploy_binding(revision, promoted, force=getattr(args, "force", False))
    return promoted


def _fetch_deploy_manifest(wanted_sha: str | None = None) -> dict:
    """Fetch a CI manifest and verify its checksum, digest, and revision.

    The interactive CLI and deploy agent share this exact artifact-validation
    path. Neither caller treats a downloaded manifest as activation authority
    until promotion binding has separately succeeded.
    """
    requested = (wanted_sha or "").lower() or None
    run_list_args = [
        "run", "list", "--repo", _DEPLOY_REPO, "--workflow", _DEPLOY_WORKFLOW,
    ]
    if requested is None or not _DEPLOY_REVISION_RE.fullmatch(requested):
        # GitHub's --commit filter requires the full head SHA. Keep the
        # documented abbreviated --sha form on the prefix-capable branch page.
        run_list_args.extend(["--branch", "main", "--limit", "15"])
    else:
        # The agent is polling an approval for a specific commit. Ask GitHub
        # for that commit directly; filtering a recent global page can lose a
        # still-retained artifact after enough newer deployments land.
        run_list_args.extend(["--commit", requested, "--limit", "15"])
    run_list_args.extend(["--json", "databaseId,headSha,status,conclusion,createdAt"])
    runs = json.loads(_gh(*run_list_args))
    run = next(
        (
            item for item in runs
            if item.get("status") == "completed" and item.get("conclusion") == "success"
            and (requested is None or item.get("headSha", "").startswith(requested))
        ),
        None,
    )
    if run is None:
        print("ERROR: no successful Deploy Blueprint run found"
              + (f" for sha {requested}" if requested else "."))
        raise APIError("no successful deploy run")
    sha = run["headSha"]
    with tempfile.TemporaryDirectory() as temporary:
        _gh(
            "run", "download", str(run["databaseId"]), "--repo", _DEPLOY_REPO,
            "-n", f"local-app-release-{sha}", "-D", temporary,
        )
        manifest_path = os.path.join(temporary, "local-app-release.json")
        with open(manifest_path, "rb") as handle:
            manifest_bytes = handle.read()
        with open(manifest_path + ".sha256", encoding="utf-8") as handle:
            expected_digest = handle.read().split()[0]
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_digest:
            print("ERROR: manifest checksum mismatch — refusing to deploy.")
            raise APIError("manifest checksum mismatch")
        manifest = json.loads(manifest_bytes)
    digest = (manifest.get("image_ref") or "").rsplit("@", 1)[-1]
    revision = manifest.get("revision") or ""
    if not _DEPLOY_DIGEST_RE.fullmatch(digest) or not _DEPLOY_REVISION_RE.fullmatch(revision):
        print("ERROR: manifest digest/revision failed validation — refusing to deploy.")
        raise APIError("manifest validation failed")
    return {"run": run, "digest": digest, "revision": revision}


def _invoke_release_shim(digest: str, revision: str, *, host: str | None) -> bool:
    """Use the root-owned release socket, locally or through SSH.

    The bootstrap removes the legacy ``/etc/sudoers.d/blueprint-release``
    grant, so a remote deploy cannot ``ssh sudo`` the shim. Instead the repo's
    socket client (scripts/release_socket_submit.py) is piped over SSH to the
    host's python3 and submits to the ACL-limited release socket — the same
    execution boundary the resident deploy agent uses.
    """
    if host is None:
        return _request_release_socket(digest, revision)
    client_path = Path(__file__).resolve().with_name("release_socket_submit.py")
    command = ["ssh", host, "python3", "-", digest, revision]
    with open(client_path, "rb") as client_source:
        proc = subprocess.run(command, check=False, stdin=client_source)
    if proc.returncode == 3:
        print(
            "Release executor accepted the handoff but its verdict was not "
            "observed; verify the served revision before retrying.",
            file=sys.stderr,
        )
        return False
    if proc.returncode != 0:
        print(f"ERROR: release exited {proc.returncode} — the release tool "
              "rolls back failed activations; inspect its receipt on the host.")
        raise APIError(f"release failed with exit code {proc.returncode}")
    return True


def _request_release_socket(digest: str, revision: str) -> bool:
    """Send the approval, reporting when execution outlives the socket wait."""
    if not _DEPLOY_REVISION_RE.fullmatch(revision):
        raise APIError("release revision failed validation")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise APIError("release digest failed validation")
    request_sent = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_RELEASE_SOCKET_TIMEOUT_SECONDS)
            connection.connect(_RELEASE_SOCKET_PATH)
            connection.sendall(f"{digest} {revision}\n".encode("ascii"))
            request_sent = True
            connection.shutdown(socket.SHUT_WR)
            response = connection.recv(256).decode("ascii", errors="replace").strip()
    except TimeoutError as exc:
        if request_sent:
            print(
                "Release executor is still running; receipt delivery is parked "
                "for the next poll.",
                file=sys.stderr,
            )
            return False
        raise APIError(f"release executor socket unavailable: {exc}") from exc
    except OSError as exc:
        if request_sent:
            print(
                "Release executor handoff was accepted but its response was "
                "lost; receipt delivery is parked for the next poll.",
                file=sys.stderr,
            )
            return False
        raise APIError(f"release executor socket unavailable: {exc}") from exc
    if response != "OK":
        detail = response.removeprefix("ERROR ") or "empty executor response"
        raise APIError(f"release executor failed: {detail}")
    return True


def _resolve_deploy_manifest(args: argparse.Namespace) -> tuple[str, str, dict]:
    """Expose the validated manifest tuple used by the standalone agent."""
    release = _fetch_deploy_manifest(getattr(args, "sha", None))
    return release["revision"], release["digest"], release["run"]


def _execute_deploy_shim(
    args: argparse.Namespace,
    revision: str,
    digest: str,
    *,
    inplace: bool = False,
) -> bool:
    """Run the canonical root-owned executor, locally or over legacy SSH."""
    return _invoke_release_shim(
        digest,
        revision,
        host=None if inplace else getattr(args, "host", None),
    )


def _execute_deploy(
    args: argparse.Namespace,
    revision: str,
    digest: str,
    promoted: dict | None,
    *,
    inplace: bool = False,
) -> None:
    """Execute the shim, then post the receipt as a distinct stage."""
    release_completed = _execute_deploy_shim(args, revision, digest, inplace=inplace)
    if release_completed is False:
        print(
            "Release is still running; activation receipt will be retried by "
            "the deploy agent.",
            file=sys.stderr,
        )
        return
    print("Release activated. Runners self-update on their next heartbeat.")
    _post_activation_receipt(args, promoted, revision)


def _read_live_approved_activation(args: argparse.Namespace) -> dict | None:
    """Return a promoted version whose approval is still actionable."""
    promoted = _read_latest_promoted_version(args)
    if promoted is None:
        return None
    promoted_sha = (promoted.get("promoted_sha") or "").lower()
    if (
        promoted.get("status") != "promoted"
        or promoted.get("serving_status") not in _ACTIVATION_REARMABLE_SERVING_STATUSES
        or not promoted.get("activation_requested_at")
        or not _DEPLOY_REVISION_RE.fullmatch(promoted_sha)
    ):
        return None
    return promoted


def _deploy_agent_once(args: argparse.Namespace) -> bool:
    """Execute one current approval and post the existing activation receipt.

    The activation intent is recorded before the shim invocation. If the
    control plane is temporarily unavailable, later polls retry only that
    receipt for the same approval instead of restarting the release.
    """
    # Keep this guard in the cycle, not only in cmd_deploy_agent: a long-lived
    # service can have its config changed after startup, and a read-only agent
    # credential must never reach the privileged executor.
    _require_write_config("deploy-agent")
    approved = _read_live_approved_activation(args)
    _write_deploy_agent_poll(getattr(args, "poll_state_path", None))
    if approved is None:
        return False
    promoted_sha = approved["promoted_sha"].lower()
    state_path = getattr(args, "state_path", None)
    pending_receipt = _read_deploy_agent_state(state_path)
    if pending_receipt is not None:
        same_activation = (
            pending_receipt.get("version_number") == approved.get("version_number")
            and pending_receipt.get("promoted_sha") == promoted_sha
            and pending_receipt.get("revision") == promoted_sha
        )
        marker_token = pending_receipt.get("activation_requested_at")
        matches_approval = same_activation and (
            marker_token == approved.get("activation_requested_at")
            or (
                "activation_requested_at" not in pending_receipt
                and approved.get("serving_status") == "unconfirmed"
            )
        )
        if matches_approval and pending_receipt.get("phase") == "failed":
            print(
                f"Approved activation for v{approved.get('version_number')} failed "
                f"before its promoted SHA was observed serving: "
                f"{pending_receipt.get('failure_reason', 'no failure reason recorded')}.",
                file=sys.stderr,
            )
            return False
        if matches_approval and pending_receipt.get("phase") == "started":
            # The started marker is the at-most-once fence. A restart after
            # this point cannot distinguish an in-flight release from one
            # that completed before the agent died, so reconcile through the
            # same serving probe used after an accepted socket handoff.
            confirmation = _pending_activation_confirmation(pending_receipt, promoted_sha)
            _write_deploy_agent_state(state_path, pending_receipt)
            if confirmation == "failed":
                print(
                    f"Approved activation for v{approved.get('version_number')} failed: "
                    "the promoted SHA was not observed serving before the deadline.",
                    file=sys.stderr,
                )
                return False
            if confirmation == "pending":
                return False
            pending_receipt["phase"] = "completed"
            _write_deploy_agent_state(state_path, pending_receipt)
            _post_activation_receipt(
                argparse.Namespace(
                    project_id=getattr(args, "project_id", None),
                    host=getattr(args, "host", "control-plane"),
                ),
                approved,
                promoted_sha,
            )
            _clear_deploy_agent_state(state_path)
            print(f"Approved activation for v{approved.get('version_number')} completed.")
            return True
        if matches_approval and pending_receipt.get("phase") in {"accepted", "completed"}:
            confirmation = _pending_activation_confirmation(pending_receipt, promoted_sha)
            _write_deploy_agent_state(state_path, pending_receipt)
            if confirmation == "failed":
                print(
                    f"Approved activation for v{approved.get('version_number')} failed: "
                    "the promoted SHA was not observed serving before the deadline.",
                    file=sys.stderr,
                )
                return False
            if confirmation == "pending":
                return False
            _post_activation_receipt(
                argparse.Namespace(
                    project_id=getattr(args, "project_id", None),
                    host=getattr(args, "host", "control-plane"),
                ),
                approved,
                promoted_sha,
            )
            _clear_deploy_agent_state(state_path)
            print(f"Approved activation for v{approved.get('version_number')} completed.")
            return True
        _clear_deploy_agent_state(state_path)

    # Approved activations are bound by the receipt endpoint to this exact
    # promoted SHA. The legacy CLI still uses the descendant binding below.
    release = _fetch_deploy_manifest(promoted_sha)
    if release["revision"] != promoted_sha:
        raise APIError("manifest revision does not match approved promoted sha")
    _validate_deploy_binding(release["revision"], approved)
    # Download can take long enough for a superseding promotion. Re-read before
    # touching the privileged shim; the receipt endpoint also rejects any race
    # that occurs during the shim invocation.
    current = _read_live_approved_activation(args)
    if (
        current is None
        or current.get("version_number") != approved.get("version_number")
        or (current.get("promoted_sha") or "").lower() != promoted_sha
        or release["revision"] != (current.get("promoted_sha") or "").lower()
    ):
        print("Approved activation changed before execution; leaving it for the next poll.")
        return False
    activation_state = {
        "version_number": current.get("version_number"),
        "promoted_sha": promoted_sha,
        "revision": release["revision"],
        "activation_requested_at": current.get("activation_requested_at"),
        "phase": "started",
        "activation_attempts": 0,
    }
    # Persist the intent before crossing the privileged boundary. This is the
    # at-most-once fence when the agent dies or the socket wait expires while
    # the root service continues the release.
    _write_deploy_agent_state(state_path, activation_state)

    def invoke_activation(attempt: int) -> bool:
        # Persist the attempt counter before crossing the privileged boundary.
        # A process restart therefore cannot silently reset a failed phase's
        # retry budget or mistake a terminal failure for a timeout.
        activation_state["activation_attempts"] = attempt
        activation_state["root_release"] = {
            "phase": "activation",
            "attempt": attempt,
            "status": "started",
        }
        _write_deploy_agent_state(state_path, activation_state)
        return _invoke_release_shim(release["digest"], release["revision"], host=None)

    try:
        release_completed = retry_activation_phase(
            "activation",
            lambda: invoke_activation(activation_state["activation_attempts"] + 1),
            retry_if=lambda exc: isinstance(exc, APIError),
            sleep=time.sleep,
        )
    except ActivationPhaseFailure as failure:
        activation_state["phase"] = "failed"
        activation_state["failure_reason"] = str(failure)
        activation_state["root_release"] = {
            "phase": failure.phase,
            "status": "failed",
            "attempts": failure.attempts,
            "failure_reason": str(failure.final_exception),
        }
        _write_deploy_agent_state(state_path, activation_state)
        print(
            f"Approved activation for v{current.get('version_number')} failed: "
            f"{failure}",
            file=sys.stderr,
        )
        return False
    if release_completed is False:
        activation_state["phase"] = "accepted"
        activation_state.setdefault("root_release", {})["status"] = "accepted"
        _write_deploy_agent_state(state_path, activation_state)
        return False
    activation_state["phase"] = "completed"
    activation_state.setdefault("root_release", {})["status"] = "completed"
    _write_deploy_agent_state(state_path, activation_state)
    confirmation = _pending_activation_confirmation(activation_state, promoted_sha)
    _write_deploy_agent_state(state_path, activation_state)
    if confirmation == "failed":
        print(
            f"Approved activation for v{current.get('version_number')} failed: "
            "the promoted SHA was not observed serving before the deadline.",
            file=sys.stderr,
        )
        return False
    if confirmation == "pending":
        return False
    _post_activation_receipt(
        argparse.Namespace(
            project_id=getattr(args, "project_id", None),
            host=getattr(args, "host", "control-plane"),
        ),
        current,
        release["revision"],
    )
    _clear_deploy_agent_state(state_path)
    print(f"Approved activation for v{current.get('version_number')} completed.")
    return True


def _activation_confirmation_now() -> datetime:
    """Return the wall-clock time used for durable activation deadlines."""
    return datetime.now(timezone.utc)


def _local_served_sha() -> str | None:
    """Return the SHA from the canonical local serving probe, or ``None``.

    ``local_app_release.py`` verifies this same loopback API endpoint as part of
    a successful release.  The deploy agent deliberately needs a fresh read
    after an accepted socket handoff: a rollback can occur after that handoff.
    Transport, HTTP, JSON, malformed-payload, and abbreviated-SHA failures are
    all unconfirmed.  The endpoint's ``sha`` is the full promoted Git SHA;
    deployment-status views may abbreviate it independently.
    """
    try:
        with urlopen(_LOCAL_SERVING_VERSION_URL, timeout=5) as response:
            if getattr(response, "status", None) != 200:
                return None
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return None
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str):
        return None
    normalized_sha = sha.lower()
    return normalized_sha if _DEPLOY_REVISION_RE.fullmatch(normalized_sha) else None


def _pending_activation_confirmation(state: dict, expected_sha: str) -> str:
    """Return ``confirmed``, ``pending``, or terminal ``failed`` for a receipt.

    The first poll after the root shim has accepted/completed an activation
    persists a deadline.  A later matching loopback SHA is the only route to a
    receipt; an old SHA, unavailable API, or malformed response remains pending
    until that deadline, then latches the local activation as failed.
    """
    expected_sha = expected_sha.lower()
    now = _activation_confirmation_now()
    raw_deadline = state.get("confirmation_deadline_at")
    try:
        deadline = datetime.fromisoformat(str(raw_deadline).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError("naive deadline")
    except (TypeError, ValueError):
        deadline = now + timedelta(seconds=_DEPLOY_AGENT_CONFIRMATION_TIMEOUT_SECONDS)
        state["confirmation_deadline_at"] = deadline.isoformat()

    served_sha = _local_served_sha()
    if served_sha == expected_sha:
        return "confirmed"
    if now >= deadline:
        state["phase"] = "failed"
        state["failure_reason"] = "served_sha_not_confirmed_before_deadline"
        state.pop("confirmation_deadline_at", None)
        return "failed"
    return "pending"


def _read_deploy_agent_state(path: str | None) -> dict | None:
    """Read the agent's host-local receipt retry marker, if it is valid."""
    if not path:
        return None
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    return state


def _write_deploy_agent_poll(path: str | None) -> None:
    """Record a successful activation poll in a separate local marker."""
    if not path:
        return
    _write_deploy_agent_state(path, {"polled_at": datetime.now(timezone.utc).isoformat()})


def _write_deploy_agent_state(path: str | None, state: dict) -> None:
    """Atomically persist an activation intent before receipt delivery."""
    if not path:
        return
    path = os.path.expanduser(path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".deploy-agent.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _clear_deploy_agent_state(path: str | None) -> None:
    """Remove a delivered or superseded receipt retry marker."""
    if not path:
        return
    try:
        os.unlink(os.path.expanduser(path))
    except FileNotFoundError:
        pass


def _preflight_file(path: str, *, executable: bool = False) -> dict:
    """Inspect one managed host file without following or changing it."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {
            "exists": False,
            "regular": False,
            "passed": False,
            "message": f"missing: {path}",
            "remediation": "run the release bootstrap on the control-plane host",
        }
    except OSError as exc:
        return {
            "exists": False,
            "regular": False,
            "passed": False,
            "message": f"cannot inspect {path}: {exc}",
            "remediation": "inspect host permissions and rerun the release bootstrap",
        }
    regular = stat.S_ISREG(info.st_mode)
    executable_ok = not executable or bool(info.st_mode & 0o111)
    root_owned = info.st_uid == 0 and info.st_gid == 0
    passed = regular and executable_ok and root_owned
    problems = []
    if not regular:
        problems.append("not a regular file")
    if executable and not executable_ok:
        problems.append("not executable")
    if not root_owned:
        problems.append(f"owned by {info.st_uid}:{info.st_gid}, expected root:root")
    result = {
        "exists": True,
        "regular": regular,
        "executable": executable_ok if executable else None,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "passed": passed,
        "message": f"present: {path}" if passed else f"{path}: {', '.join(problems)}",
    }
    if not passed:
        result["remediation"] = (
            "run the release bootstrap on the control-plane host and verify root:root ownership"
        )
    return result


def _systemd_preflight() -> dict:
    """Read the deploy-agent unit and its live systemd state."""
    file_check = _preflight_file(_DEPLOY_AGENT_UNIT_PATH)
    if not file_check["passed"]:
        file_check["message"] = f"unit file: {file_check['message']}"
        return {
            "path": _DEPLOY_AGENT_UNIT_PATH,
            "unit": os.path.basename(_DEPLOY_AGENT_UNIT_PATH),
            "enabled": False,
            "active": False,
            **file_check,
        }
    unit = os.path.basename(_DEPLOY_AGENT_UNIT_PATH)
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemctl", "show", unit,
                "--property=UnitFileState",
                "--property=ActiveState",
                "--property=SubState",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return {
            "path": _DEPLOY_AGENT_UNIT_PATH,
            "unit": unit,
            "enabled": False,
            "active": False,
            "passed": False,
            "message": f"cannot query systemd: {exc}",
            "remediation": "run systemctl daemon-reload and enable --now blueprint-deploy-agent.service",
        }
    properties = {}
    for line in (result.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    enabled = properties.get("UnitFileState") == "enabled"
    active = properties.get("ActiveState") == "active"
    passed = result.returncode == 0 and enabled and active
    response = {
        "path": _DEPLOY_AGENT_UNIT_PATH,
        "unit": unit,
        "enabled": enabled,
        "active": active,
        "active_state": properties.get("ActiveState"),
        "sub_state": properties.get("SubState"),
        "unit_file_state": properties.get("UnitFileState"),
        "passed": passed,
        "message": (
            "installed, enabled, and active"
            if passed else
            f"enabled={properties.get('UnitFileState', 'unknown')}, "
            f"active={properties.get('ActiveState', 'unknown')}"
        ),
    }
    if not passed:
        response["remediation"] = (
            "run systemctl daemon-reload && systemctl enable --now "
            "blueprint-deploy-agent.service"
        )
    return response


def _poll_preflight(path: str) -> dict:
    """Read the last successful poll marker and assess its age."""
    marker = _read_deploy_agent_state(path)
    polled_at = marker.get("polled_at") if marker else None
    if not polled_at:
        return {
            "path": path,
            "last_poll_at": None,
            "seconds_since_last_poll": None,
            "passed": False,
            "message": "no successful poll has ever occurred",
            "remediation": "start or restart blueprint-deploy-agent.service",
        }
    try:
        parsed = datetime.fromisoformat(str(polled_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except (TypeError, ValueError):
        return {
            "path": path,
            "last_poll_at": polled_at,
            "seconds_since_last_poll": None,
            "passed": False,
            "message": f"invalid successful-poll marker: {polled_at}",
            "remediation": "restart blueprint-deploy-agent.service to recreate its poll marker",
        }
    passed = age <= _DEPLOY_AGENT_POLL_MAX_AGE_SECONDS
    result = {
        "path": path,
        "last_poll_at": parsed.isoformat(),
        "seconds_since_last_poll": round(age, 1),
        "passed": passed,
        "message": f"last successful poll {age:.1f}s ago",
    }
    if not passed:
        result["remediation"] = "restart blueprint-deploy-agent.service and inspect its journal"
    return result


def _preflight_api_failure(exc: Exception) -> tuple[str, str]:
    """Classify auth failures separately from transport/control-plane failures."""
    message = str(exc)
    if re.search(r"HTTP (401|403)\b", message):
        return "unauthorized", (
            "control plane rejected credentials; refresh BLUEPRINT_API_KEY or the agent token"
        )
    return "unreachable", (
        "control plane is unreachable; verify network access and the configured API URL"
    )


def _activation_preflight(args: argparse.Namespace) -> tuple[dict, dict]:
    """Read the activation endpoint once and derive all four approval predicates."""
    try:
        project_id = get_project_id(getattr(args, "project_id", None))
        path = f"/api/admin/projects/{project_id}/release-versions?status=promoted&limit=1"
        payload = api("GET", path)
    except Exception as exc:  # noqa: BLE001 — diagnostic must report all config/transport failures
        status, remediation = _preflight_api_failure(exc)
        failure = {
            "passed": False,
            "status": status,
            "message": str(exc),
            "remediation": remediation,
        }
        return failure, {
            "pending": False,
            "preconditions": {
                "status_promoted": False,
                "serving_unconfirmed": False,
                "activation_requested_at": False,
                "promoted_sha_valid": False,
            },
            "passed": False,
            "message": "not checked because the activation read failed",
            "remediation": "restore control-plane access, then rerun preflight",
        }
    versions = payload.get("versions") if isinstance(payload, dict) else None
    version = versions[0] if versions else None
    control = {
        "passed": True,
        "status": "reachable_authenticated",
        "message": "activation endpoint reachable and authenticated",
        "endpoint": path,
    }
    preconditions = {
        "status_promoted": bool(version and version.get("status") == "promoted"),
        "serving_unconfirmed": bool(
            version
            and version.get("serving_status")
            in _ACTIVATION_REARMABLE_SERVING_STATUSES
        ),
        "activation_requested_at": bool(version and version.get("activation_requested_at")),
        "promoted_sha_valid": bool(
            version and _DEPLOY_REVISION_RE.fullmatch((version.get("promoted_sha") or "").lower())
        ),
    }
    pending = bool(version and version.get("activation_requested_at"))
    passed = not pending or all(preconditions.values())
    activation = {
        "pending": pending,
        "version_number": version.get("version_number") if version else None,
        "preconditions": preconditions,
        "passed": passed,
        "message": (
            "installed, polling, nothing to do"
            if not pending else
            "approved activation is pending and all executor preconditions are satisfied"
            if passed else
            "activation request is present but one or more executor preconditions failed"
        ),
    }
    if not passed:
        activation["remediation"] = "repair the promoted activation state, then rerun preflight"
    return control, activation


def _cmd_deploy_agent_preflight(args: argparse.Namespace) -> int:
    """Run the deploy-agent diagnostic without taking any activation action."""
    service = _systemd_preflight()
    polling = _poll_preflight(getattr(args, "poll_state_path", _DEPLOY_AGENT_POLL_PATH))
    shim = _preflight_file(_RELEASE_SHIM_PATH, executable=True)
    if not shim["passed"]:
        shim["message"] = f"release shim: {shim['message']}"
    control, activation = _activation_preflight(args)
    state_path = getattr(args, "state_path", _DEPLOY_AGENT_STATE_PATH)
    parked = _read_deploy_agent_state(state_path) is not None
    state = {
        "path": state_path,
        "parked_for_receipt_retry": parked,
        "passed": not parked,
        "message": (
            "no interrupted activation is parked"
            if not parked else
            "interrupted activation is parked awaiting receipt retry"
        ),
    }
    if parked:
        state["remediation"] = "restore control-plane access and run deploy-agent once to retry the receipt"
    checks = {
        "service": service,
        "polling": polling,
        "release_shim": shim,
        "control_plane": control,
        "activation": activation,
        "state": state,
    }
    report = {"passed": all(check.get("passed", False) for check in checks.values()), "checks": checks}
    _emit(args, report, lambda data: _render_deploy_agent_preflight(data))
    return 0 if report["passed"] else 1


def _render_deploy_agent_preflight(data: dict) -> None:
    """Render a concise, actionable human-readable preflight checklist."""
    print("=== DEPLOY AGENT PREFLIGHT ===")
    labels = {
        "service": "systemd unit",
        "polling": "polling",
        "release_shim": "release shim",
        "control_plane": "control-plane access",
        "activation": "approved activation",
        "state": "crash-safe state",
    }
    for name, check in data["checks"].items():
        status = "PASS" if check.get("passed") else "FAIL"
        print(f"[{status}] {labels[name]}: {check.get('message', '')}")
        if name == "activation":
            for predicate, satisfied in check.get("preconditions", {}).items():
                print(f"       {predicate}: {'satisfied' if satisfied else 'not satisfied'}")
        if not check.get("passed") and check.get("remediation"):
            print(f"       Remediation: {check['remediation']}")


def _acquire_deploy_agent_lock(path: str):
    """Acquire the systemd-owned runtime lock without blocking another poll."""
    if fcntl is None:
        raise APIError(
            "deploy-agent requires POSIX advisory locking; run it on the Linux "
            "systemd host (all other blueprint commands support Windows)"
        )
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - returned locked handle
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def cmd_deploy_agent(args: argparse.Namespace) -> int | None:
    """Run the local approved-activation executor at a 30-second default cadence."""
    if getattr(args, "preflight", False):
        return _cmd_deploy_agent_preflight(args)
    if args.poll_interval <= 0:
        raise APIError("poll interval must be positive")
    _require_write_config("deploy-agent")
    lock = _acquire_deploy_agent_lock(args.lock_path)
    if lock is None:
        print("deploy-agent: another activation poll is already running; skipping.")
        return
    try:
        while True:
            try:
                _deploy_agent_once(args)
            except (APIError, OSError, json.JSONDecodeError) as exc:
                print(f"deploy-agent: transient poll failure: {exc}", file=sys.stderr)
                if args.once and isinstance(exc, APIError):
                    raise
            if args.once:
                return
            time.sleep(args.poll_interval)
    finally:
        lock.close()


def _post_activation_receipt(
    args: argparse.Namespace,
    promoted: dict | None,
    revision: str,
) -> None:
    """POST the activation receipt so the backend confirms the promoted version.

    Called only AFTER the SSH activation succeeds.  The receipt carries the
    promoted ``version_number``, the activated ``revision``, and the SSH host;
    the backend stamps it on ``last_qualification`` and resolves the version's
    serving state.  A failed POST exits non-zero: the release IS activated, but
    the backend was not told, so the serving-confirmation loop would otherwise
    keep waiting on a stale signal.
    """
    if promoted is None:
        return
    try:
        result = api("POST", (
            f"/api/admin/projects/{get_project_id(getattr(args, 'project_id', None))}"
            "/activation-receipt"
        ), {
            "version_number": promoted.get("version_number"),
            "revision": revision,
            "host": args.host,
        })
    except APIError:
        print("ERROR: release activated, but the activation receipt could not "
              "be recorded — the promoted version's serving confirmation will "
              "not reflect this activation. Investigate the backend and re-post "
              "the receipt (or re-run deploy) to close the loop.", file=sys.stderr)
        raise
    if isinstance(result, dict):
        serving = result.get("serving_status")
        served = result.get("served_sha") or ""
        print(f"  Receipt:   v{promoted.get('version_number')} "
              f"serving_status={serving} (served_sha={served[:12] or '?'})")
    else:
        print(f"  Receipt:   recorded for v{promoted.get('version_number')}")


def cmd_deploy(args: argparse.Namespace) -> None:
    """Activate the newest CI-built main release on the local control plane.

    Chain: find the newest successful "Deploy Blueprint" run on main (or the
    run for --sha), download its integrity-checked local-app-release manifest,
    bind the built revision to the project's latest promoted release version
    (SHA-or-descendant via the GitHub compare API; --force bypasses), then
    invoke the root-owned zero-argument-trust shim on the control-plane host
    over SSH. After activation, POST an activation receipt so the backend
    records the served revision and closes the serving-confirmation loop.

    The shim (scripts/blueprint_release.py, installed as
    /usr/local/sbin/blueprint-release) re-validates both values against strict
    regexes, pins the registry/repository, and the immutable release tool
    re-verifies digest, revision label, and health with automatic rollback —
    so this client is a convenience, not a trust boundary.
    """
    release = _fetch_deploy_manifest(args.sha)
    run = release["run"]
    digest = release["digest"]
    revision = release["revision"]

    print("=== LOCAL CONTROL-PLANE DEPLOY ===")
    print(f"  Host:      {args.host}")
    print(f"  Revision:  {revision}")
    print(f"  Image:     ...@{digest[:19]}...")
    print(f"  Built:     {run.get('createdAt', '?')} (run {run['databaseId']})")
    promoted = _resolve_deploy_binding(args, revision)
    if args.dry_run:
        print("  (dry run — nothing deployed)")
        return
    _require_write_config("deploy")
    if not args.yes:
        answer = input("Activate this release? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return
    _execute_deploy(args, revision, digest, promoted)


_DISPATCH = {
    "projects": cmd_projects,
    "agent-access": cmd_agent_access,
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
    "deploy": cmd_deploy,
    "migrate": cmd_migrate,
    "deploy-agent": cmd_deploy_agent,
}


def main(argv: list | None = None) -> int:
    # Windows consoles default to cp1252; force UTF-8 so non-ASCII glyphs never
    # crash output (mirrors scripts/ops.py).
    # stderr carries every ERROR/note line, including project and task names
    # that may be non-ASCII; leaving it on cp1252 turns a clear diagnostic into
    # a UnicodeEncodeError traceback.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    handler = _DISPATCH[args.command]
    try:
        # ServiceCredentials are machine principals, not ambient admin keys.
        # Fence the complete CLI before project resolution or handler dispatch so
        # a denied command cannot reach subprocess, Docker, gcloud, git, SSH, or
        # even an unintended API route. This runs BEFORE the local-only fast path
        # below: a local verb still performs host work, so "needs no credential"
        # must not mean "escapes the fence". The fence is a no-op when no scoped
        # credential is selected, including when no config resolves at all.
        _enforce_scoped_cli_command(args)
        if _is_local_only_cli_command(args):
            # `compute run` is documented as "no orchestrator, no DB, no GCP".
            # Skip project/task-id resolution — every one of those paths calls
            # load_config()/api(), which is why a local run died on a missing
            # Blueprint config it never needed.
            local_result = handler(args)
            return local_result if isinstance(local_result, int) else 0
        # Resolve --project <name> to an id once, up front, so every
        # get_project_id(args.project_id) call site targets it. An explicit
        # --project-id wins if both are supplied.
        proj_name = getattr(args, "project_name", None)
        if proj_name and not getattr(args, "project_id", None):
            args.project_id = resolve_project_id_by_name(proj_name)
        elif getattr(args, "project_id", None):
            # An explicit --project-id that names nothing used to produce an
            # empty listing, which reads as "no tasks" rather than "no project".
            validate_project_id(args.project_id)
        # Every id this CLI prints is truncated to 8 chars, so resolve a prefix
        # back to its full uuid here -- one choke point ahead of the handlers,
        # the same place --project is resolved -- instead of at each of the
        # subcommands that take a task id.
        raw_task_id = getattr(args, "task_id", None)
        if task_id_needs_prefix_resolution(raw_task_id):
            args.task_id = resolve_task_id_by_prefix(
                raw_task_id, get_project_id(getattr(args, "project_id", None))
            )
        result = handler(args)
        if isinstance(result, int):
            return result
    except APIError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
