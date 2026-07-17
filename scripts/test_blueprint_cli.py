"""Smoke tests for the blueprint operator CLI.

Verifies the argparse tree parses (every subcommand exposes --help), that the
module imports cleanly, and that --json emits valid JSON for a read command —
all with blueprint_api.api mocked so no network/credentials are needed.
"""
import argparse
import io
import json
import os
import subprocess
import urllib.error
import urllib.request
import pytest

import blueprint
import blueprint_api


def test_get_project_id_matches_configured_default_repo(monkeypatch):
    """Unscoped commands target the configured repo, not API list position."""
    monkeypatch.delenv("BLUEPRINT_PROJECT_ID", raising=False)
    monkeypatch.setattr(
        blueprint_api,
        "load_config",
        lambda: {"default_repo": "HenryHamster/BlueprintAIStudio"},
    )
    monkeypatch.setattr(
        blueprint_api,
        "list_projects",
        lambda: [
            {
                "id": "playground-id",
                "name": "HenryHamster/playground",
                "github_owner": "HenryHamster",
                "github_repo": "playground",
            },
            {
                "id": "blueprint-id",
                "name": "Blueprint",
                "github_owner": "HenryHamster",
                "github_repo": "BlueprintAIStudio",
            },
        ],
    )
    monkeypatch.setattr(blueprint_api, "_project_id_cache", None)

    assert blueprint_api.get_project_id() == "blueprint-id"


def test_get_project_id_refuses_ambiguous_unconfigured_fallback(
    monkeypatch, capsys
):
    """Multiple projects without a repo/id selection must never pick first."""
    monkeypatch.delenv("BLUEPRINT_PROJECT_ID", raising=False)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {})
    monkeypatch.setattr(
        blueprint_api,
        "list_projects",
        lambda: [
            {"id": "newest", "name": "Playground"},
            {"id": "intended", "name": "Blueprint"},
        ],
    )
    monkeypatch.setattr(blueprint_api, "_project_id_cache", None)

    with pytest.raises(SystemExit):
        blueprint_api.get_project_id()

    assert "multiple projects" in capsys.readouterr().err.lower()

# ── root wrapper existence and invocation tests ───────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_blueprint_wrapper_exists_and_is_executable():
    """./blueprint exists at the repo root and is executable."""
    wrapper = os.path.join(REPO_ROOT, "blueprint")
    assert os.path.isfile(wrapper), f"./blueprint not found at {wrapper}"
    assert os.access(wrapper, os.X_OK), "./blueprint is not executable"


def test_check_wrapper_exists_and_is_executable():
    """./check exists at the repo root and is executable."""
    wrapper = os.path.join(REPO_ROOT, "check")
    assert os.path.isfile(wrapper), f"./check not found at {wrapper}"
    assert os.access(wrapper, os.X_OK), "./check is not executable"


def test_blueprint_wrapper_help_exits_zero():
    """./blueprint --help exits 0 and emits CLI output."""
    wrapper = os.path.join(REPO_ROOT, "blueprint")
    result = subprocess.run(
        [wrapper, "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"./blueprint --help exited {result.returncode}: {result.stderr}"
    assert len(result.stdout) > 0, "./blueprint --help produced no output"


def test_check_test_delegates_to_standalone_harness():
    """./check test script delegates to scripts/test-backend.sh --standalone --no-postgres."""
    check_path = os.path.join(REPO_ROOT, "check")
    with open(check_path) as f:
        content = f.read()
    assert "scripts/test-backend.sh" in content, "./check does not reference scripts/test-backend.sh"
    assert "--standalone" in content, "./check does not pass --standalone to scripts/test-backend.sh"
    assert "--no-postgres" in content, "./check does not pass --no-postgres to scripts/test-backend.sh"


def test_blueprint_wrapper_shell_syntax():
    """./blueprint passes bash -n (no syntax errors)."""
    wrapper = os.path.join(REPO_ROOT, "blueprint")
    result = subprocess.run(["bash", "-n", wrapper], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed on ./blueprint: {result.stderr}"


def test_check_wrapper_shell_syntax():
    """./check passes bash -n (no syntax errors)."""
    wrapper = os.path.join(REPO_ROOT, "check")
    result = subprocess.run(["bash", "-n", wrapper], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed on ./check: {result.stderr}"


def test_install_cli_shell_syntax():
    """scripts/install-cli.sh passes bash -n (no syntax errors)."""
    script = os.path.join(REPO_ROOT, "scripts", "install-cli.sh")
    result = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed on scripts/install-cli.sh: {result.stderr}"


def test_blueprint_wrapper_resolves_via_symlink(tmp_path):
    """./blueprint works when invoked via a symlink from an unrelated directory.

    This test would FAIL under the old `dirname "$0"` form and only passes with
    `readlink -f` — making the assertion load-bearing for the symlink fix.
    """
    wrapper = os.path.join(REPO_ROOT, "blueprint")
    link = tmp_path / "blueprint"
    link.symlink_to(wrapper)
    result = subprocess.run(
        [str(link), "--help"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        f"blueprint --help via symlink exited {result.returncode}: {result.stderr}"
    )
    assert len(result.stdout) > 0, "blueprint --help via symlink produced no output"


# ── --help for every (sub)command parses and exits 0 ─────────────────────────

HELP_INVOCATIONS = [
    [],
    ["status"],
    ["diagnostics"],
    ["ops"],
    ["ops", "repair"],
    ["ops", "reset-health"],
    ["ops", "requeue-failed"],
    ["ops", "restart-orchestrator"],
    ["ops", "circuit-breaker"],
    ["tasks"],
    ["tasks", "list"],
    ["tasks", "show"],
    ["tasks", "cancel"],
    ["tasks", "requeue"],
    ["tasks", "scope"],
    ["tasks", "tree"],
    ["tasks", "failures"],
    ["tasks", "evaluating"],
    ["tasks", "cancel-all"],
    ["tasks", "requeue-ci"],
    ["tasks", "trigger-rollup"],
    ["tasks", "complete"],
    ["tasks", "set-status"],
    ["tasks", "reset-iteration-cap"],
    ["tasks", "archive"],
    ["tasks", "research"],
    ["tasks", "autonomy-debug"],
    ["versions"],
    ["versions", "list"],
    ["versions", "show"],
    ["versions", "export-changelog"],
    ["system"],
    ["system", "repair"],
    ["system", "restart"],
    ["system", "zones"],
    ["system", "autonomy"],
    ["monitor"],
    ["monitor", "alerts"],
    ["monitor", "digest"],
    ["monitor", "activity"],
    ["trials"],
    ["promote-recover"],
    ["directions"],
    ["directions", "list"],
    ["directions", "show"],
    ["directions", "add"],
    ["directions", "update"],
    ["directions", "unhold"],
    ["directions", "retarget"],
    ["directions", "activate"],
    ["directions", "cancel"],
    ["logs"],
    ["logs", "errors"],
    ["logs", "tail"],
    ["logs", "search"],
    ["logs", "task"],
    ["messages"],
    ["messages", "list"],
    ["messages", "send"],
    ["messages", "answer"],
    ["messages", "confirm"],
    ["messages", "confirm", "list"],
    ["messages", "confirm", "accept"],
    ["messages", "confirm", "dismiss"],
    ["fleet"],
    ["fleet", "pool"],
    ["fleet", "warm"],
    ["fleet", "accounts"],
    ["fleet", "zones"],
    ["envelope"],
    ["envelope", "show"],
    ["envelope", "set"],
    ["knowledge"],
    ["knowledge", "list"],
    ["knowledge", "show"],
    ["knowledge", "sweep"],
    ["compute"],
    ["compute", "list"],
    ["compute", "show"],
    ["compute", "kill"],
    ["compute", "analyze"],
    ["compute", "run"],
    ["compute", "launch"],
    ["tasks", "create"],
]


@pytest.mark.parametrize("argv", HELP_INVOCATIONS)
def test_help_parses_and_exits_zero(argv):
    parser = blueprint.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv + ["--help"])
    assert exc.value.code == 0


def test_module_builds_full_parser():
    """The full parser tree constructs without error."""
    parser = blueprint.build_parser()
    assert parser.prog == "blueprint"


def test_no_duplicate_top_level_defs():
    """Each CLI module must not define the same top-level name twice.

    Rollup merges can silently concatenate duplicate defs; Python binds the
    last one, so the first (often correct) definition is silently shadowed.
    """
    import ast
    import pathlib

    scripts_dir = pathlib.Path(__file__).parent
    for module_name in ("blueprint.py", "blueprint_api.py"):
        source = (scripts_dir / module_name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=module_name)
        seen: dict = {}
        dups: list = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
                if name in seen:
                    dups.append(
                        f"{name} (first at line {seen[name]}, again at line {node.lineno})"
                    )
                else:
                    seen[name] = node.lineno
        assert not dups, (
            f"{module_name} has duplicate top-level definitions: {', '.join(dups)}"
        )


def test_envelope_registered_once():
    """The 'envelope' subcommand is registered exactly once in the parser."""
    parser = blueprint.build_parser()
    # Get the subparsers action which contains all top-level subcommands
    subparsers_action = None
    for action in parser._subparsers._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers_action = action
            break

    assert subparsers_action is not None, "No subparsers found in parser"

    # Count how many times 'envelope' appears in the subcommand choices
    choices = subparsers_action.choices
    envelope_count = sum(1 for choice in choices.keys() if choice == 'envelope')

    # Verify envelope is registered exactly once
    assert envelope_count == 1, (
        f"Expected 'envelope' to be registered exactly once, but found {envelope_count} "
        f"registrations. Available subcommands: {sorted(choices.keys())}"
    )

    # Verify the envelope parser is configured correctly
    assert 'envelope' in choices, "'envelope' not found in subcommand choices"
    envelope_parser = choices['envelope']
    assert envelope_parser is not None
    assert envelope_parser.prog == 'blueprint envelope'


# ── --json produces valid JSON for a read command ───────────────────────────

@pytest.fixture
def mock_api(monkeypatch):
    """Mock blueprint_api.api and project resolution; record the calls made."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if "/admin/diagnostics" in path:
            return {
                "stuck_tasks": [
                    {"task_id": "aabbccdd-0000-0000-0000-000000000000",
                     "title": "stuck task", "reason": "HEARTBEAT_STALE",
                     "stuck_for_seconds": 3600},
                    {"task_id": "eeff0011-0000-0000-0000-000000000000",
                     "title": "another stuck", "reason": "HEARTBEAT_STALE",
                     "stuck_for_seconds": 900},
                ],
                "circuit_breakers": {
                    "proj-1": {"state": "CLOSED", "failure_count": 0,
                               "cooldown_ends_at": None},
                },
                "direction_remediation": [],
                "total_stuck_count": 2,
                "queued_count": 2,
                "in_progress_count": 5,
                "warnings": [],
            }
        if "/admin/state" in path:
            return {
                "deploy": {
                    "status": "RUNNING",
                    "current_sha": "abc",
                    "current_revision": "rev-1",
                    "deployed_at": None,
                    "pause_reason": None,
                    "rollback_reason": None,
                    "rollback_count": 0,
                    "last_known_good_sha": "",
                    "halted": False,
                },
                "version": {
                    "version_number": 12,
                    "phase": "build",
                    "status": "open",
                    "opened_at": None,
                    "phase_changed_at": None,
                    "goal": None,
                    "merged_task_count": 3,
                    "in_flight_task_count": 0,
                    "qualification_state": {"state": "qualifying"},
                    "serving": {
                        "status": None,
                        "sha": "abc123",
                        "confirmed_at": None,
                        "failure_reason": None,
                        "artifacts": [],
                    },
                },
                "fleet": {
                    "active": 0, "queued": 0, "evaluating": 0,
                    "parked": 0, "stale_heartbeat_count": 0,
                },
                "generated_at": "2026-06-15T00:00:00Z",
            }
        if "version-lifecycle" in path:
            return {
                "current_version": {
                    "version_number": 12, "phase": "build",
                    "qualification_state": "qualifying", "merged_task_count": 3,
                    "active_candidate_sha": "abc123",
                    "tasks_merged": [
                        {"task_type": "create", "title": "Add widget feature", "pr_number": 101},
                        {"task_type": "fix", "title": "Fix null pointer", "pr_number": 102},
                        {"task_type": "create", "title": "New dashboard panel", "pr_number": 103},
                    ],
                    "aggregate_serving_status": "SERVING",
                },
                "recent_versions": [
                    {"id": "uuid-v11", "version_number": 11, "phase": "stabilize",
                     "status": "promoted", "promoted_sha": "deadbeef",
                     "merged_task_count": 10, "merged_count": 10,
                     "highlights": {"headline": "promoted & serving",
                                    "merged_count": 10, "proven": 8,
                                    "total_merged_for_acceptance": 10,
                                    "qualification_confidence": None,
                                    "qualification_threshold": None}},
                ],
                "promotion_enabled_effective": True,
                "promotion_enabled_source": "fleet",
            }
        if "/release-versions?" in path or path.endswith("/release-versions"):
            # Paginated full-history endpoint. Echo the requested window so the
            # CLI's offset/limit handling and header/footer can be asserted.
            from urllib.parse import urlparse, parse_qs

            qs = parse_qs(urlparse(path).query)
            limit = int(qs.get("limit", ["20"])[0])
            offset = int(qs.get("offset", ["0"])[0])
            status_f = qs.get("status", [None])[0]
            all_rows = [
                {"id": "uuid-v4", "version_number": 4, "phase": "build",
                 "status": "open", "merged_count": 2,
                 "highlights": {"headline": "open in build"}},
                {"id": "uuid-v3", "version_number": 3, "phase": "stabilize",
                 "status": "promoted", "promoted_sha": "cafe", "merged_count": 9,
                 "highlights": {"headline": "promoted v3"}},
                {"id": "uuid-v2", "version_number": 2, "phase": "build",
                 "status": "abandoned", "merged_count": 1,
                 "highlights": {"headline": "abandoned v2"}},
                {"id": "uuid-v1", "version_number": 1, "phase": "build",
                 "status": "promoted", "promoted_sha": "f00d", "merged_count": 5,
                 "highlights": {"headline": "promoted v1"}},
            ]
            if status_f:
                all_rows = [r for r in all_rows if r["status"] == status_f]
            total = len(all_rows)
            window = all_rows[offset:offset + limit]
            return {"versions": window, "total": total,
                    "limit": limit, "offset": offset}
        if "/release-versions/uuid-v11/summary" in path:
            return {
                "phase": "stabilize",
                "goal": "Stabilize the codebase",
                "merged_count": 10,
                "acceptance_aggregate": {"proven": 8, "total": 10},
                "tasks_merged": [
                    {"task_type": "create", "title": "Shipped feature A", "pr_number": 90},
                    {"task_type": "refactor", "title": "Clean up module", "pr_number": 91},
                ],
                "highlights": {"headline": "10 tasks merged; focus on stability"},
                "serving_status": "SERVING",
            }
        if "/release-versions/5/summary" in path:
            return {
                "version_number": 5,
                "phase": "build",
                "status": "promoted",
                "merged_count": 7,
                "acceptance_aggregate": {"proven": 6, "total": 7},
                "merged_task_count": 7,
                "tasks_merged": [
                    {"task_type": "fix", "title": "Old fix from v5", "pr_number": 50},
                    {"task_type": "create", "title": "Old feature from v5", "pr_number": 51},
                ],
                "highlights": {"headline": "7 tasks merged; early version"},
                "serving_status": "SERVING",
            }
        if "/tasks/" in path and method == "PATCH":
            tid = path.split("/tasks/")[1].split("?")[0]
            new_status = (body or {}).get("status", "CANCELLED")
            return {"id": tid, "status": new_status}
        if "/admin/digest" in path:
            return {
                "completed_count": 15, "failed_count": 2, "prs_opened": 8,
                "research_cycles": 3, "pending_approval_count": 1, "active_count": 5,
                "running_count": 2, "queued_count": 3,
                "needs_attention": [], "recent_completions": [], "recent_failures": [],
                "capability_adoption_verdicts": {
                    "repo_map": {
                        "verdict": "adopt",
                        "lift": 1.25,
                        "arm_composition": {
                            "with_assigned": 45,
                            "without_assigned": 38,
                        },
                        "accrual_projection": {
                            "with_7d": 7,
                            "without_7d": 6,
                            "window_days": 7,
                            "holdback_fraction": 0.3,
                            "days_to_min_n": 5.5,
                            "alternative_projections": {
                                "0.1": 2.1,
                                "0.3": 5.5,
                                "0.5": 8.3,
                            },
                        },
                        "containment_rate": 0.95,
                        "delivery_rate": 0.98,
                    },
                },
                "window_hours": 24,
            }
        if "/max-plan-accounts/" in path and method == "PATCH":
            acct_id = path.split("/max-plan-accounts/")[1]
            enabled = (body or {}).get("enabled", True)
            return {"id": acct_id, "name": "evan", "enabled": enabled}
        if "/max-plan-accounts/" in path and method == "DELETE":
            return {"message": "removed"}
        if "/warm-vm-state" in path:
            return {
                "pool_metrics": {
                    "enabled": True,
                    "target_size": 5,
                    "current_size": 3,
                    "idle_count": 2,
                    "idle_ttl_secs": 300,
                    "project_allowlist": [],
                    "projected_idle_cost_usd": 0.5,
                    "settled_idle_cost_usd": 0.4,
                    "live_idle_cost_usd": 0.1,
                    "cost_guard": {
                        "engaged": False, "hit_rate": 0.8, "min_hit_rate": 0.5,
                        "raw_hit_rate": 0.7, "total_dispatches": 10,
                        "raw_total_dispatches": 15, "idle_cost_usd": 0.5,
                        "min_idle_cost_usd": 0.1, "reason": None,
                    },
                    "funnel": {
                        "window_hours": 24, "eligible_hit_rate": 0.8,
                        "pool_hit_rate": 0.7, "pool_lease_hits": 8,
                        "eligible_dispatches": 10, "total_dispatches": 15,
                        "cold_dispatches": 5, "lease_miss_total": 2,
                        "eligible_miss_total": 2,
                        "lease_miss_by_reason": {"no_idle_vm": 2},
                        "by_account": {},
                    },
                },
                "warm_pin_metrics": {
                    "pool_circuit_breaker_suppressed": False,
                    "pool_suppressed_until": None,
                    "pool_claim_failures_last_hour": 0,
                    "pool_failure_rate_threshold": 5,
                    "active_pool_origin_claims": 1,
                    "pool_origin_share": 0.2,
                },
            }
        if "/zone-health" in path:
            return {
                "us-central1-a": {"status": "OK", "last_success": "2026-06-17T00:00:00Z",
                                   "consecutive_failures": 0},
                "us-east1-b": {"status": "DEGRADED", "last_success": "2026-06-16T00:00:00Z",
                                "consecutive_failures": 2},
            }
        if "/admin/settings" in path:
            return {
                "max_concurrent_vms": 10, "promotion_enabled": True,
                "daily_cost_ceiling_usd": 50.0,
                "max_plan_accounts": [
                    {"id": "acct-0001-abcd", "name": "henry", "enabled": True,
                     "active_tasks": 2, "rl_hit_count": 0, "disabled_models": {}},
                    {"id": "acct-0002-efgh", "name": "evan", "enabled": False,
                     "active_tasks": 0, "rl_hit_count": 1, "disabled_models": {}},
                ],
            }
        if "/messages/" in path and method == "PATCH":
            msg_id = path.split("/messages/")[1].split("?")[0]
            return {"id": msg_id, "status": "resolved"}
        if "/pending-confirm/" in path and "/accept" in path and method == "POST":
            pending_id = path.split("/pending-confirm/")[1].split("/accept")[0]
            return {"status": "accepted", "pending_id": pending_id, "type": "major_decision"}
        if "/pending-confirm/" in path and "/dismiss" in path and method == "POST":
            pending_id = path.split("/pending-confirm/")[1].split("/dismiss")[0]
            return {"status": "dismissed", "pending_id": pending_id}
        if path.rstrip("/").endswith("/projects/proj-1") and method == "GET":
            return {
                "id": "proj-1",
                "project_name": "test-project",
                "problem_model": {
                    "pending_user_confirmation": [
                        {"id": "pend-abc123", "type": "major_decision",
                         "proposal": "Should we adopt the new auth flow?",
                         "rationale": "Current flow has scaling issues",
                         "created_at": "2026-06-15T10:00:00Z"},
                        {"id": "pend-def456", "type": "intent_change",
                         "proposal": "Shift focus to mobile-first design",
                         "rationale": "User feedback indicates mobile priority",
                         "created_at": "2026-06-14T08:00:00Z"},
                    ]
                },
            }
        if "/messages" in path and method == "POST":
            return {"id": "msg-abc", "status": "created", "arming": "not_armed"}
        if "/messages" in path:
            return [
                {"id": "msg-001", "type": "FEEDBACK", "content": "please fix the CI",
                 "from": "operator", "status": "open",
                 "created_at": "2026-06-15T10:00:00Z"},
                {"id": "msg-002", "type": "QUESTION", "content": "what is blocked?",
                 "from": "system", "status": "open",
                 "created_at": "2026-06-15T09:00:00Z"},
            ]
        if "/tasks/" in path:
            return {"id": "task-123", "title": "demo", "task_type": "create",
                    "status": "IN_PROGRESS", "created_at": "2026-06-15T00:00:00Z"}
        if "/tasks" in path and "status=FAILED" in path:
            return {
                "tasks": [
                    {"id": "fail-aaa0-0000-0000-000000000000",
                     "task_type": "create", "title": "failed create task",
                     "status": "FAILED", "attempt_count": 2,
                     "parent_task_id": None,
                     "latest_status_message": "API usage exhausted (acct: abc123)",
                     "error_logs": ""},
                    {"id": "fail-bbb0-0000-0000-000000000000",
                     "task_type": "fix", "title": "failed fix task",
                     "status": "FAILED", "attempt_count": 1,
                     "parent_task_id": "parent-0000-0000-0000-000000000000",
                     "latest_status_message": "spawn E2BIG: arg list too long",
                     "error_logs": "spawn E2BIG: arg list too long"},
                    {"id": "fail-ccc0-0000-0000-000000000000",
                     "task_type": "create", "title": "another api exhausted",
                     "status": "FAILED", "attempt_count": 3,
                     "parent_task_id": None,
                     "latest_status_message": "API usage exhausted (acct: xyz789)",
                     "error_logs": ""},
                ],
                "total": 3,
            }
        if "/tasks" in path and "status=EVALUATING" in path:
            return {
                "tasks": [
                    {"id": "eval-aaa0-0000-0000-000000000000",
                     "task_type": "research", "title": "long running research",
                     "status": "EVALUATING",
                     "context": {"review_loop": {"ci_waiting": True, "pr_number": 42}},
                     "latest_status_message": "waiting for CI on PR #42"},
                    {"id": "eval-bbb0-0000-0000-000000000000",
                     "task_type": "create", "title": "create task evaluating",
                     "status": "EVALUATING",
                     "context": {},
                     "latest_status_message": ""},
                ],
                "total": 2,
            }
        if "/autonomy-debug" in path:
            return {
                "spawn_blocked": True,
                "blocking_guards": [
                    {"guard": "rate_limit", "reason": "tasks_per_hour=10 >= max=10"},
                ],
                "directions": [
                    {"id": "dir-001", "text": "improve performance", "is_eligible": False,
                     "seconds_until_eligible": 3600},
                    {"id": "dir-002", "text": "add new feature", "is_eligible": True},
                ],
            }
        if "/tasks" in path and "status=QUEUED" in path:
            return {"tasks": [
                {"id": "q-task-111", "task_type": "create", "title": "queued task",
                 "status": "QUEUED", "context": {}},
            ], "total": 1}
        if "/tasks" in path and "limit=500" in path:
            # Failure-class filtering raises the internal fetch limit to 500 and
            # narrows client-side; return a mixed set carrying failure_class.
            return {
                "tasks": [
                    {"id": "fc-code-0000-0000-000000000000",
                     "task_type": "create", "title": "code error task",
                     "status": "FAILED", "failure_class": "CODE_ERROR"},
                    {"id": "fc-timeout-00-0000-000000000000",
                     "task_type": "fix", "title": "timeout task",
                     "status": "FAILED", "failure_class": "AGENT_TIMEOUT"},
                    {"id": "fc-none-0000-0000-000000000000",
                     "task_type": "create", "title": "running task",
                     "status": "IN_PROGRESS", "failure_class": None},
                ],
                "total": 3,
            }
        if "/tasks" in path:
            return {"tasks": [{"id": "task-123", "task_type": "create",
                               "status": "IN_PROGRESS", "title": "demo"}],
                    "total": 1}
        if "/admin/alerts" in path:
            return [
                {"id": "alert-1", "severity": "WARNING", "title": "High memory usage",
                 "message": "orchestrator memory > 80%", "created_at": "2026-06-17T12:00:00Z",
                 "task_id": None},
                {"id": "alert-2", "severity": "ERROR", "title": "API rate limited",
                 "message": "Anthropic API rate limit hit", "created_at": "2026-06-17T11:30:00Z",
                 "task_id": "task-abc"},
            ]
        if "/admin/activity" in path:
            return {
                "events": [
                    {"type": "TASK_COMPLETED", "title": "fix: handle edge case",
                     "task_id": "task-001", "timestamp": "2026-06-17T12:15:00Z",
                     "pr_url": "https://github.com/repo/pull/123", "duration_seconds": 1800},
                    {"type": "TASK_FAILED", "title": "feat: new widget",
                     "task_id": "task-002", "timestamp": "2026-06-17T11:45:00Z",
                     "pr_url": "", "duration_seconds": None,
                     "error_summary": "API usage exhausted"},
                ],
                "total": 2,
            }
        if "/admin/task-cost-breakdown" in path:
            return {
                "project_id": "proj-1",
                "window": "1h",
                "window_ms": 3600000,
                "task_count": 5,
                "total_estimated_cost_usd": 0.42,
                "task_type_breakdown": [
                    {"task_type": "create", "task_count": 3, "estimated_cost_usd": 0.30},
                    {"task_type": "fix", "task_count": 2, "estimated_cost_usd": 0.12},
                ],
                "direction_breakdown": [],
                "top_tasks": [
                    {"task_id": "aabbccdd-0000-0000-0000-000000000000",
                     "task_title": "feat: add widget", "task_type": "create",
                     "direction_id": None, "completed_at": "2026-06-18T00:00:00",
                     "estimated_cost_usd": 0.15},
                ],
                "truncated": False,
            }
        if "/admin/task-duration-stats" in path:
            return {
                "stats": {"create": {"p50_seconds": 600, "sample_count": 10}},
                "scope_duration_stats": {
                    "small": {"p50_seconds": 400, "p90_seconds": 800, "sample_size": 8},
                },
                "phase_duration_stats": {},
            }
        if "/admin/direction-auto-pause-stats" in path:
            return {
                "window_hours": 24,
                "auto_paused_count": 2,
                "auto_recovered_count": 1,
                "auto_retired_count": 0,
                "currently_auto_paused": 1,
                "top_reasons": [{"reason": "stale_tasks", "count": 2}],
                "trend": "flat",
                "prev_window_auto_paused_count": 2,
            }
        if "/resource-envelope" in path and method == "GET":
            return {
                "project_id": "proj-test-uuid",
                "max_concurrent_vms": 4,
                "spend_ceiling_usd": 50.0,
                "spend_window_days": 7,
                "max_concurrent_compute_jobs": 2,
                "active_vm_count": 1,
                "recent_spend_usd": 12.34,
                "recent_spend_compute_usd": 8.0,
                "recent_spend_agent_vm_usd": 4.0,
                "recent_spend_idle_usd": 0.34,
                "is_throttled": False,
            }
        if "/resource-envelope" in path and method == "PATCH":
            return {
                "project_id": "proj-test-uuid",
                "max_concurrent_vms": 6,
                "spend_ceiling_usd": 100.0,
                "spend_window_days": 7,
                "max_concurrent_compute_jobs": 2,
                "active_vm_count": 1,
                "recent_spend_usd": 12.34,
                "recent_spend_compute_usd": 8.0,
                "recent_spend_agent_vm_usd": 4.0,
                "recent_spend_idle_usd": 0.34,
                "is_throttled": False,
            }
        if "/admin/failing-task-types" in path:
            return {
                "gated_types": [
                    {"task_type": "create", "failure_rate": 0.75,
                     "sample_size": 5, "threshold": 0.7},
                ],
                "global_failure_threshold": 0.8,
                "anomaly_tightened": False,
                "window_minutes": 15,
                "generated_at": "2026-06-18T00:00:00",
            }
        if "/knowledge-documents/" in path and method == "GET":
            doc_id = path.split("/knowledge-documents/")[1].split("?")[0]
            return {
                "id": doc_id,
                "title": "Research on test coverage",
                "source_kind": "web_research",
                "direction_id": "d-abc12345",
                "created_at": "2026-06-15T00:00:00Z",
                "topic": "testing",
                "tags": ["coverage", "reliability"],
                "summary": "Key findings on test coverage practices.",
                "body": "## Findings\n\nTest coverage is important for reliability.",
                "source_citations": [
                    {"ref": "RFC-001", "url": "https://example.com/rfc-001", "title": "Test coverage guide"},
                    "https://example.com/plain-url",
                ],
            }
        if "/knowledge-documents" in path and method == "GET":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(path).query)
            docs = [
                {"id": "kdoc-001", "title": "API Rate Limiting", "source_kind": "web_research",
                 "direction_id": "d-abc12345", "created_at": "2026-06-15T00:00:00Z",
                 "topic": "api", "tags": ["rate-limiting", "resilience"],
                 "sweep_id": None, "arm_label": None},
                {"id": "kdoc-002", "title": "Design Patterns", "source_kind": "design",
                 "direction_id": "d-def67890", "created_at": "2026-06-14T00:00:00Z",
                 "topic": "architecture", "tags": [],
                 "sweep_id": None, "arm_label": None},
                {"id": "kdoc-arm-a", "title": "Sweep arm alpha", "source_kind": "compute_experiment",
                 "direction_id": "d-sweep01", "created_at": "2026-06-20T00:00:00Z",
                 "topic": "experiment", "tags": [],
                 "sweep_id": "swp-test001", "arm_label": "alpha",
                 "forward_quality_verdict": {
                     "criterion_met": True,
                     "cost_usd": 1.25,
                     "result_metrics": {"accuracy": 0.92},
                 }},
                {"id": "kdoc-arm-b", "title": "Sweep arm beta", "source_kind": "compute_experiment",
                 "direction_id": "d-sweep01", "created_at": "2026-06-20T01:00:00Z",
                 "topic": "experiment", "tags": [],
                 "sweep_id": "swp-test001", "arm_label": "beta",
                 "forward_quality_verdict": {
                     "criterion_met": False,
                     "cost_usd": 1.10,
                     "result_metrics": {"accuracy": 0.81},
                 }},
            ]
            direction_f = qs.get("direction", [None])[0]
            source_kind_f = qs.get("source_kind", [None])[0]
            q_f = qs.get("q", [None])[0]
            tag_f = qs.get("tag", [None])[0]
            sweep_id_f = qs.get("sweep_id", [None])[0]
            if direction_f:
                docs = [d for d in docs if d.get("direction_id") == direction_f]
            if source_kind_f:
                docs = [d for d in docs if d.get("source_kind") == source_kind_f]
            if q_f:
                docs = [d for d in docs if q_f.lower() in (d.get("title") or "").lower()]
            if tag_f:
                docs = [d for d in docs if tag_f in (d.get("tags") or [])]
            if sweep_id_f:
                docs = [d for d in docs if d.get("sweep_id") == sweep_id_f]
            return {"documents": docs, "total": len(docs)}
        if "/compute-jobs/" in path and "/kill" in path and method == "POST":
            job_id = path.split("/compute-jobs/")[1].split("/")[0]
            return {"compute_job": {"id": job_id, "status": "CANCELLED"}}
        if "/compute-jobs/" in path and "/analyze" in path and method == "POST":
            job_id = path.split("/compute-jobs/")[1].split("/")[0]
            return {"outcome": "success", "knowledge_document_id": "kdoc-abc123", "job_id": job_id}
        if "/compute-jobs/" in path and method == "GET":
            # Detail endpoint: GET /api/admin/compute-jobs/{id}
            job_id = path.split("/compute-jobs/")[1].split("/")[0]
            _ALL_MOCK_JOBS = [
                {"id": "cjob-aaa0-0000-0000-000000000001",
                 "status": "RUNNING", "task_title": "GPU benchmark alpha",
                 "task_type": "compute_experiment",
                 "final_cost_usd": None,
                 "experiment": "exp-alpha",
                 "hypothesis": "GPU is faster",
                 "criterion_verdict": None,
                 "success_criterion": {"metric": "latency_ms", "operator": "lt", "threshold": 100}},
                {"id": "cjob-bbb0-0000-0000-000000000002",
                 "status": "SUCCEEDED", "task_title": "GPU benchmark beta",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 12.5,
                 "experiment": "exp-beta",
                 "hypothesis": "Quantization works",
                 "criterion_verdict": {"met": True, "metric": "latency_ms", "operator": "lt", "threshold": 100, "value": 80.0, "measurable": True},
                 "polling_metadata": {"result_metrics": {"latency_ms": 80.0, "arms": {"arm_a": {"latency_ms": 75.0}, "arm_b": {"latency_ms": 85.0}}},
                                      "command_log": {"text": "Starting GPU benchmark...\nRunning test 1\nTest passed", "truncated": False}},
                 "success_criterion": {"metric": "latency_ms", "operator": "lt", "threshold": 100}},
                {"id": "cjob-ccc0-0000-0000-000000000003",
                 "status": "SUCCEEDED", "task_title": "Arm transcript test",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 5.2,
                 "experiment": "exp-gamma",
                 "criterion_verdict": {"met": True, "metric": "score", "operator": "gt", "threshold": 0.8, "value": 0.95, "measurable": True},
                 "polling_metadata": {"result_metrics": {"score": 0.95, "arms": {"baseline": {"score": 0.85, "label": "baseline version", "note": "original implementation", "log": "baseline test output\nall checks passed"}, "optimized": {"score": 0.95, "label": "optimized version", "note": "after refactoring", "log": "optimized test output\nperformance improved"}}},
                                      "command_log": {"text": "Setup phase complete\nRunning comprehensive benchmark\nResults collected\nAnalysis complete", "truncated": False}},
                 "success_criterion": {"metric": "score", "operator": "gt", "threshold": 0.8}},
                {"id": "cjob-old0-0000-0000-000000000201",
                 "status": "SUCCEEDED", "task_title": "Old job beyond 200 cap",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 3.0,
                 "criterion_verdict": {"met": True, "metric": "score", "operator": "gt", "threshold": 0.5, "value": 0.9, "measurable": True},
                 "success_criterion": {"metric": "score", "operator": "gt", "threshold": 0.5}},
                {"id": "cjob-ddd0-0000-0000-000000000004",
                 "status": "SUCCEEDED", "task_title": "List-arms job",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 7.5,
                 "experiment": "exp-list-arms",
                 "criterion_verdict": {"met": True, "metric": "score", "operator": "gt", "threshold": 0.7, "value": 0.88, "measurable": True},
                 "polling_metadata": {"result_metrics": {"score": 0.88, "arms": [
                     {"label": "variant-a", "note": "first variant", "log": "variant-a run log\nall checks passed", "score": 0.82},
                     {"label": "variant-b", "note": "second variant", "log": "variant-b run log\nperformance improved", "score": 0.88},
                 ]}},
                 "success_criterion": {"metric": "score", "operator": "gt", "threshold": 0.7}},
                {"id": "cjob-eee0-0000-0000-000000000005",
                 "status": "SUCCEEDED", "task_title": "Failed arms test",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 2.1,
                 "experiment": "exp-failed",
                 "criterion_verdict": {"met": False, "metric": "latency", "operator": "lt", "threshold": 100, "value": None, "measurable": False},
                 "polling_metadata": {"result_metrics": {"arms": {"passing": {"latency": 75.0}, "crashed": {"exit_code": 1, "error": "arm result file not written (arm may have failed)"}, "no_metric": {"exit_code": 0, "error": "metric 'latency' not found in result"}}},
                                      "command_log": {"text": "Running experiment\nArm 1 passed\nArm 2 crashed\nArm 3 completed but no metric", "truncated": False}},
                 "success_criterion": {"metric": "latency", "operator": "lt", "threshold": 100}},
                {"id": "cjob-fff0-0000-0000-000000000006",
                 "status": "FAILED", "task_title": "Failed list-arm test",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 3.5,
                 "experiment": "exp-failed-list-arm",
                 "criterion_verdict": None,
                 "polling_metadata": {"result_metrics": {"arms": [
                     {"name": "arm_ok", "score": 0.85},
                     {"name": "arm_timeout", "exit_code": 124,
                      "reason": "arm timed out (exit 124): wall-clock limit exceeded",
                      "stderr_excerpt": "Timeout after 3600s"},
                 ]}},
                 "success_criterion": {"metric": "score", "operator": "gt", "threshold": 0.8}},
                {"id": "cjob-ggg0-0000-0000-000000000007",
                 "status": "FAILED", "task_title": "Failed arm test",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 4.0,
                 "experiment": "exp-failed-arm",
                 "criterion_verdict": None,
                 "polling_metadata": {"result_metrics": {"arms": {
                     "arm_ok": {"score": 0.91},
                     "arm_crash": {"exit_code": 1, "error": "arm crashed (exit 1): result file not written",
                                   "stderr_excerpt": "Traceback (most recent call last):\n  ...\nRuntimeError: CUDA OOM"},
                 }}},
                 "success_criterion": {"metric": "score", "operator": "gt", "threshold": 0.8}},
                {"id": "cjob-hhh0-0000-0000-000000000008",
                 "status": "FAILED", "task_title": "Provisioning failure test",
                 "task_type": "compute_experiment",
                 "final_cost_usd": 0.0,
                 "experiment": "exp-prov-fail",
                 "terminal_reason": "VM provisioning failed: insufficient quota for machine_type n2-standard-32 in zone us-central1-a",
                 "vm_phase": "provisioning",
                 "launch_contract": {"requested_deadline_seconds": 7200, "provisioning_model": "SPOT"},
                 "criterion_verdict": None,
                 "success_criterion": {"metric": "latency", "operator": "lt", "threshold": 100}},
            ]
            job = next((j for j in _ALL_MOCK_JOBS if j["id"] == job_id), None)
            if job is None:
                raise Exception(f"404 compute job not found: {job_id}")
            return {"compute_job": job}
        if "/compute-jobs" in path:
            return {
                "compute_jobs": [
                    {"id": "cjob-aaa0-0000-0000-000000000001",
                     "status": "RUNNING", "task_title": "GPU benchmark alpha",
                     "task_type": "compute_experiment",
                     "final_cost_usd": None,
                     "started_at": "2020-01-01T00:00:00Z",
                     "accrued_cost_usd": 5.0,
                     "experiment": "exp-alpha",
                     "benchmark": "bench-1",
                     "hypothesis": "GPU is faster",
                     "criterion_verdict": None,
                     "success_criterion": {"metric": "latency_ms", "operator": "lt", "threshold": 100}},
                    {"id": "cjob-bbb0-0000-0000-000000000002",
                     "status": "SUCCEEDED", "task_title": "GPU benchmark beta",
                     "task_type": "compute_experiment",
                     "final_cost_usd": 12.5,
                     "experiment": "exp-beta",
                     "benchmark": "bench-2",
                     "hypothesis": "Quantization works",
                     "criterion_verdict": {"met": True, "metric": "latency_ms", "operator": "lt", "threshold": 100, "value": 80.0, "measurable": True},
                     "polling_metadata": {"result_metrics": {"latency_ms": 80.0, "arms": {"arm_a": {"latency_ms": 75.0}, "arm_b": {"latency_ms": 85.0}}},
                                          "command_log": {"text": "Starting GPU benchmark...\nRunning test 1\nTest passed", "truncated": False}},
                     "success_criterion": {"metric": "latency_ms", "operator": "lt", "threshold": 100}},
                    {"id": "cjob-ccc0-0000-0000-000000000003",
                     "status": "SUCCEEDED", "task_title": "Arm transcript test",
                     "task_type": "compute_experiment",
                     "final_cost_usd": 5.2,
                     "experiment": "exp-gamma",
                     "criterion_verdict": {"met": True, "metric": "score", "operator": "gt", "threshold": 0.8, "value": 0.95, "measurable": True},
                     "polling_metadata": {"result_metrics": {"score": 0.95, "arms": {"baseline": {"score": 0.85, "label": "baseline version", "note": "original implementation", "log": "baseline test output\nall checks passed"}, "optimized": {"score": 0.95, "label": "optimized version", "note": "after refactoring", "log": "optimized test output\nperformance improved"}}},
                                          "command_log": {"text": "Setup phase complete\nRunning comprehensive benchmark\nResults collected\nAnalysis complete", "truncated": False}},
                     "success_criterion": {"metric": "score", "operator": "gt", "threshold": 0.8}},
                    {"id": "cjob-eee0-0000-0000-000000000005",
                     "status": "SUCCEEDED", "task_title": "Failed arms test",
                     "task_type": "compute_experiment",
                     "final_cost_usd": 2.1,
                     "experiment": "exp-failed",
                     "criterion_verdict": {"met": False, "metric": "latency", "operator": "lt", "threshold": 100, "value": None, "measurable": False},
                     "polling_metadata": {"result_metrics": {"arms": {"passing": {"latency": 75.0}, "crashed": {"exit_code": 1, "error": "arm result file not written (arm may have failed)"}, "no_metric": {"exit_code": 0, "error": "metric 'latency' not found in result"}}},
                                          "command_log": {"text": "Running experiment\nArm 1 passed\nArm 2 crashed\nArm 3 completed but no metric", "truncated": False}},
                     "success_criterion": {"metric": "latency", "operator": "lt", "threshold": 100}},
                ],
                "has_more": False,
                "limit": 20,
                "offset": 0,
                "summary": {
                    "total_jobs": 2,
                    "active_jobs": 1,
                    "terminal_jobs": 1,
                    "total_spend_usd": 12.5,
                    "criterion_measured": 1,
                    "criterion_met": 1,
                    "outcome_breakdown": {"success": 1},
                },
            }
        if "/directions/" in path and "/unhold" in path:
            return {"status": "unheld", "direction_id": "d-abc12345", "was_held": True}
        if "/directions/" in path and method == "DELETE":
            return {"message": "Direction removed", "direction_id": "d-staged12345", "cancelled_tasks": 0}
        if "/directions/" in path and method == "PATCH":
            if body and "staged_for_version" in body:
                return {"direction": {"id": "d-staged12345", "text": "Stage the next release",
                                      "priority": "medium", "status": "staged",
                                      "staged_for_version": body["staged_for_version"],
                                      "staged_at": "2026-06-18T12:00:00Z"}}
            if body and body.get("status") == "active":
                return {"direction": {"id": "d-staged12345", "text": "Stage the next release",
                                      "priority": "medium", "status": "active"}}
            return {"direction": {"id": "d-abc12345", "text": "Updated text",
                                  "priority": "high", "status": "active"}}
        if "/directions/" in path and method == "POST":
            return {"direction": {"id": "d-abc12345", "text": "New direction",
                                  "priority": "medium", "status": "active"}}
        if "/directions" in path and method == "POST":
            staged = bool(body and body.get("staged"))
            return {"direction": {"id": "d-newdir01", "text": "New direction",
                                  "priority": "medium",
                                  "status": "staged" if staged else "active",
                                  "staged_for_version": 13 if staged else None}}
        if "/directions" in path:
            return {
                "directions": [
                    {"id": "d-abc12345", "text": "Improve test coverage",
                     "status": "active", "priority": "high",
                     "research_history": [{"researched_at": "2026-06-01", "tasks_created": 2, "prs_merged": 1}],
                     "notes": [{"text": "operator note", "added_at": "2026-06-01T00:00:00"}],
                     "held": False, "possibly_complete": False, "possibly_saturated": False},
                    {"id": "d-staged12345", "text": "Stage the next release",
                     "status": "staged", "priority": "medium",
                     "staged_for_version": 13, "staged_at": "2026-06-18T12:00:00Z",
                     "research_history": [], "notes": [], "held": False,
                     "possibly_complete": False, "possibly_saturated": False},
                    {"id": "d-def67890", "text": "Refactor legacy modules",
                     "status": "paused", "priority": "medium",
                     "research_history": [], "notes": [], "held": True,
                     "possibly_complete": False, "possibly_saturated": False},
                ]
            }
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    return calls


def test_status_json_is_valid_json(mock_api, capsys):
    rc = blueprint.main(["--json", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["project_id"] == "proj-1"
    assert parsed["current_version"]["version_number"] == 12
    assert parsed["current_version"]["active_candidate_sha"] == "abc123"
    # promotion fields come from version-lifecycle when available
    assert parsed["promotion"]["enabled_effective"] is not None
    assert parsed["promotion"]["enabled_effective"] is True
    # state and lifecycle calls are made; no deploy-state or settings calls
    assert any("/admin/state" in path for _, path, _ in mock_api)
    assert any("version-lifecycle" in path for _, path, _ in mock_api)
    assert not any("deploy-state" in path for _, path, _ in mock_api)
    assert not any("settings" in path for _, path, _ in mock_api)


def test_status_surfaces_fleet_and_serving(mock_api, capsys):
    """status --json exposes fleet block and version serving truth from state snapshot."""
    rc = blueprint.main(["--json", "status"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    # fleet block is present at the top level
    assert "fleet" in parsed
    assert parsed["fleet"]["active"] == 0
    assert parsed["fleet"]["queued"] == 0
    assert parsed["fleet"]["stale_heartbeat_count"] == 0
    # halted is surfaced from deploy
    assert parsed["deploy_state"]["halted"] is False
    # in-flight and serving fields are surfaced from version
    cv = parsed["current_version"]
    assert cv["in_flight_task_count"] == 0
    assert cv["serving_status"] is None  # fixture has serving.status=None
    assert cv["serving_confirmed_at"] is None


def test_status_json_flag_after_subcommand(mock_api, capsys):
    """--json is accepted both before and after the subcommand."""
    rc = blueprint.main(["status", "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["deploy_state"]["status"] == "RUNNING"


def test_status_human_render_includes_fleet(mock_api, capsys):
    """Human-readable status output includes a FLEET section with active count."""
    rc = blueprint.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "FLEET" in out
    assert "active=0" in out


def test_status_error_path(monkeypatch, capsys):
    """When /api/admin/state fails, errors dict is populated and no crash occurs."""
    calls = []

    def fail_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if "/admin/state" in path:
            raise RuntimeError("connection refused")
        return {}

    monkeypatch.setattr(blueprint_api, "api", fail_api)
    monkeypatch.setattr(blueprint, "api", fail_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["--json", "status"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "state" in parsed["errors"]
    assert "connection refused" in parsed["errors"]["state"]
    assert parsed["current_version"] is None
    # fleet key is present even on error (with None values from empty state)
    assert "fleet" in parsed


def test_diagnostics_json(mock_api, capsys):
    """diagnostics --json surfaces stuck-task, circuit-breaker, and grouped-failure
    sections, with grouped_failures derived client-side from the fetched stuck tasks."""
    rc = blueprint.main(["--json", "diagnostics"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)

    # stuck_tasks passes through as a list from the API
    assert isinstance(parsed["stuck_tasks"], list)
    assert len(parsed["stuck_tasks"]) == 2

    # circuit_breakers passes through as a dict keyed by project id
    assert isinstance(parsed["circuit_breakers"], dict)
    assert parsed["circuit_breakers"]["proj-1"]["state"] == "CLOSED"

    # grouped_failures is computed client-side by reason — both stuck tasks
    # share HEARTBEAT_STALE, so it collapses to one key with two task ids
    assert isinstance(parsed["grouped_failures"], dict)
    assert "HEARTBEAT_STALE" in parsed["grouped_failures"]
    assert len(parsed["grouped_failures"]["HEARTBEAT_STALE"]) == 2

    # summary counts come straight from the diagnostics payload
    assert parsed["total_stuck"] == 2
    assert parsed["queued_count"] == 2
    assert parsed["in_progress_count"] == 5

    # exactly one diagnostics GET — grouping must not trigger extra API calls
    diag_calls = [p for m, p, _ in mock_api if "/admin/diagnostics" in p]
    assert len(diag_calls) == 1


def test_diagnostics_groups_by_stuck_reason_field(mock_api, monkeypatch, capsys):
    """Grouping falls back to the live API's `stuck_reason` field when `reason`
    is absent, so triage works against the deployed endpoint shape."""
    def fake_api(method, path, body=None, timeout=30):
        return {
            "stuck_tasks": [
                {"task_id": "11", "title": "a", "stuck_reason": "no_heartbeat"},
                {"task_id": "22", "title": "b", "stuck_reason": "long_queued"},
            ],
            "circuit_breakers": {},
            "total_stuck_count": 2,
            "queued_count": 0,
            "in_progress_count": 0,
            "warnings": [],
        }

    monkeypatch.setattr(blueprint, "api", fake_api)
    rc = blueprint.main(["--json", "diagnostics"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert set(parsed["grouped_failures"]) == {"no_heartbeat", "long_queued"}


def test_diagnostics_error_path(monkeypatch, capsys):
    """A diagnostics endpoint failure degrades to an error dict, not a crash."""
    def fail_api(method, path, body=None, timeout=30):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(blueprint, "api", fail_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["--json", "diagnostics"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "diagnostics" in parsed["errors"]
    assert "connection refused" in parsed["errors"]["diagnostics"]
    assert parsed["stuck_tasks"] == []
    assert parsed["grouped_failures"] == {}


def test_tasks_list_json(mock_api, capsys):
    rc = blueprint.main(["--json", "tasks", "list", "--status", "IN_PROGRESS"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["total"] == 1
    assert parsed["tasks"][0]["id"] == "task-123"
    # the status filter must have been threaded into the request path
    assert any("status=IN_PROGRESS" in path for _, path, _ in mock_api)


def test_tasks_list_blocked_flag(mock_api, capsys):
    """`tasks list --blocked` threads status=BLOCKED into the request."""
    rc = blueprint.main(["--json", "tasks", "list", "--blocked"])
    assert rc == 0
    assert any("status=BLOCKED" in path for _, path, _ in mock_api)


def test_tasks_list_blocked_appends_to_status(mock_api, capsys):
    """--blocked appends BLOCKED rather than replacing an explicit --status."""
    rc = blueprint.main(["--json", "tasks", "list", "--status", "QUEUED", "--blocked"])
    assert rc == 0
    paths = [path for _, path, _ in mock_api]
    assert any("status=QUEUED" in p and "status=BLOCKED" in p for p in paths)


def test_tasks_list_approval_needed_flag(mock_api, capsys):
    """`tasks list --approval-needed` threads approval_needed=true."""
    rc = blueprint.main(["--json", "tasks", "list", "--approval-needed"])
    assert rc == 0
    assert any("approval_needed=true" in path for _, path, _ in mock_api)


def test_tasks_list_offset(mock_api, capsys):
    """`tasks list --offset 50` threads offset=50 into the request."""
    rc = blueprint.main(["--json", "tasks", "list", "--offset", "50"])
    assert rc == 0
    assert any("offset=50" in path for _, path, _ in mock_api)


def test_tasks_list_offset_zero_omitted(mock_api, capsys):
    """offset=0 (the default) is not appended to the request path."""
    rc = blueprint.main(["--json", "tasks", "list"])
    assert rc == 0
    assert not any("offset=" in path for _, path, _ in mock_api)


def test_tasks_list_filters(mock_api, capsys):
    """`--failure-class CODE_ERROR` keeps only matching tasks and raises the fetch limit."""
    rc = blueprint.main(["--json", "tasks", "list", "--failure-class", "CODE_ERROR"])
    assert rc == 0
    # internal fetch limit raised to 500 so the client-side filter has rows
    assert any("limit=500" in path for _, path, _ in mock_api)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["filtered_client_side"] is True
    ids = [t["id"] for t in parsed["tasks"]]
    assert ids == ["fc-code-0000-0000-000000000000"]
    assert all(t["failure_class"] == "CODE_ERROR" for t in parsed["tasks"])


def test_tasks_list_failure_class_repeatable(mock_api, capsys):
    """Two --failure-class values union-filter the returned list."""
    rc = blueprint.main(["--json", "tasks", "list",
                         "--failure-class", "CODE_ERROR",
                         "--failure-class", "AGENT_TIMEOUT"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    ids = sorted(t["id"] for t in parsed["tasks"])
    assert ids == ["fc-code-0000-0000-000000000000",
                   "fc-timeout-00-0000-000000000000"]


def test_tasks_list_failure_class_warns_on_impossible_status(mock_api, capsys):
    """--failure-class with a non-FAILED/CANCELLED status warns on stderr."""
    rc = blueprint.main(["--json", "tasks", "list", "--status", "IN_PROGRESS",
                         "--failure-class", "CODE_ERROR"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "IN_PROGRESS" in err


def test_tasks_list_failure_class_no_warn_for_failed_status(mock_api, capsys):
    """No warning when --failure-class is paired with FAILED."""
    rc = blueprint.main(["--json", "tasks", "list", "--status", "FAILED",
                         "--failure-class", "CODE_ERROR"])
    assert rc == 0
    assert "WARNING" not in capsys.readouterr().err


def test_tasks_list_invalid_failure_class_rejected(mock_api, capsys):
    """An unknown --failure-class value errors at parse time (SystemExit 2)."""
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "list", "--failure-class", "NOT_A_CLASS"])
    assert exc.value.code == 2


def test_tasks_show_json(mock_api, capsys):
    rc = blueprint.main(["--json", "tasks", "show", "task-123"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["id"] == "task-123"


def test_versions_show_json(mock_api, capsys):
    rc = blueprint.main(["--json", "versions", "show"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["version_number"] == 12
    assert parsed["phase"] == "build"
    assert isinstance(parsed["tasks_merged"], list)
    assert len(parsed["tasks_merged"]) == 3
    assert parsed["tasks_merged"][0]["pr_number"] == 101


def test_versions_show_changelog_human_render(mock_api, capsys):
    """Human-readable versions show includes task titles and PR numbers."""
    rc = blueprint.main(["versions", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#101" in out
    assert "Add widget feature" in out


def test_versions_show_historical_fetches_summary(mock_api, capsys):
    """versions show <N> for a historical version fetches the per-version summary."""
    rc = blueprint.main(["--json", "versions", "show", "11"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["version_number"] == 11
    assert isinstance(parsed["tasks_merged"], list)
    # historical fixture returns tasks via the /summary endpoint
    assert parsed["tasks_merged"][0]["pr_number"] == 90
    # the /summary endpoint must have been called
    assert any("/release-versions/" in p and "/summary" in p
               for _, p, _ in mock_api)


def test_versions_show_historical_includes_phase_goal_merged_acceptance(mock_api, capsys):
    """versions show <N> for a closed version includes phase, goal, merged_tasks,
    and acceptance from the summary endpoint."""
    rc = blueprint.main(["--json", "versions", "show", "11"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["version_number"] == 11
    # These fields should come from the /summary endpoint
    assert parsed["phase"] == "stabilize"
    assert parsed["goal"] == "Stabilize the codebase"
    assert parsed["merged_task_count"] == 10
    assert parsed["acceptance_aggregate"] == {"proven": 8, "total": 10}


def test_versions_show_historical_human_render(mock_api, capsys):
    """Human-readable versions show <N> prints phase, goal, merged_tasks, and acceptance."""
    rc = blueprint.main(["versions", "show", "11"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "phase:" in out
    assert "stabilize" in out
    assert "goal:" in out
    assert "Stabilize the codebase" in out
    assert "merged_tasks:" in out
    assert "10" in out
    assert "acceptance:" in out


def test_versions_show_fallback_for_old_versions(mock_api, capsys):
    """versions show <N> for versions older than the 5-version window uses fallback endpoint."""
    rc = blueprint.main(["--json", "versions", "show", "5"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["version_number"] == 5
    assert parsed["phase"] == "build"
    assert parsed["status"] == "promoted"
    assert parsed["merged_task_count"] == 7
    # The fallback endpoint should have been called for version 5
    assert any("/release-versions/5/summary" in p for _, p, _ in mock_api)


def test_tasks_version_flag_emits_notice(mock_api, capsys):
    """--version is unsupported by the API and must emit an explicit notice."""
    rc = blueprint.main(["tasks", "list", "--version", "11"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "version filtering is unsupported" in err


def test_promote_recover_aborts_without_confirmation(mock_api, monkeypatch, capsys):
    """A mutating action without --yes on non-interactive stdin must not fire."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["promote-recover", "qualify"])
    assert rc == 0
    # no POST to /qualify should have been issued
    assert not any("qualify" in path for method, path, _ in mock_api if method == "POST")


def test_promote_recover_read_only_config_refuses(mock_api, monkeypatch):
    """Mutating actions refuse under a read-only (agent-JWT) config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["promote-recover", "qualify", "--yes"])
    assert exc.value.code == 1


def test_promote_recover_qualify_fires_with_yes(mock_api, monkeypatch, capsys):
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["--json", "promote-recover", "qualify", "--yes"])
    assert rc == 0
    assert any(method == "POST" and "qualify" in path for method, path, _ in mock_api)


def test_promote_recover_cut_discloses_staged_activation_consequences(monkeypatch):
    """promote-recover cut should disclose lifecycle consequences before confirm."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if method == "GET" and "version-lifecycle" in path:
            return {
                "current_version": {
                    "version_number": 12,
                    "phase": "build",
                    "in_flight_task_count": 3,
                    "qualification_state": {
                        "state": "promotion_in_flight",
                        "promotion_pr_number": 456,
                    },
                },
                "staged_directions": [
                    {"id": "dir-1", "text": "Ship staged change A", "staged_for_version": 12},
                    {"id": "dir-2", "text": "Ship staged change B", "staged_for_version": None},
                    {"id": "dir-3", "text": "Ignore later stage", "staged_for_version": 99},
                ],
            }
        if method == "POST" and "version-cut" in path:
            raise AssertionError("cut mutation should not fire when user declines")
        raise AssertionError(f"unexpected API call: {method} {path}")

    prompts = []
    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    rc = blueprint.main(["promote-recover", "cut"])
    assert rc == 0
    assert prompts, "expected a confirmation prompt"
    prompt = prompts[0]
    assert "Start 2 staged directions immediately" in prompt
    assert "Ship staged change A" in prompt
    assert "Ship staged change B" in prompt
    assert "3 in-flight tasks will NOT be stopped" in prompt
    assert "Promotion PR #456 is open and auto-merging" in prompt
    assert not any(method == "POST" and "version-cut" in path for method, path, _ in calls)


def test_promote_recover_cut_falls_back_when_lifecycle_read_fails(monkeypatch):
    """promote-recover cut uses the generic confirmation when lifecycle fetch fails."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if method == "GET" and "version-lifecycle" in path:
            raise RuntimeError("boom")
        if method == "POST" and "version-cut" in path:
            raise AssertionError("cut mutation should not fire when user declines")
        raise AssertionError(f"unexpected API call: {method} {path}")

    prompts = []
    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    rc = blueprint.main(["promote-recover", "cut"])
    assert rc == 0
    assert prompts, "expected a confirmation prompt"
    assert prompts[0] == "About to abandon the open version and open the next. Proceed? [y/N] "
    assert not any(method == "POST" and "version-cut" in path for method, path, _ in calls)


def test_ops_requeue_failed_aborts_without_confirmation(mock_api, monkeypatch, capsys):
    """ops requeue-failed without --yes on non-interactive stdin must not fire."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["ops", "requeue-failed"])
    assert rc == 0
    # no PATCH to tasks should have been issued
    assert not any("/tasks/" in path and method == "PATCH" for method, path, _ in mock_api)
    err = capsys.readouterr().err
    assert "Refusing to mutate without --yes" in err


def test_ops_requeue_failed_read_only_config_refuses(mock_api, monkeypatch):
    """ops requeue-failed refuses under a read-only (agent-JWT) config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["ops", "requeue-failed", "--yes"])
    assert exc.value.code == 1


def test_ops_requeue_failed_fires_with_yes(mock_api, monkeypatch, capsys):
    """ops requeue-failed with --yes fires the mutation and emits human summary."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["ops", "requeue-failed", "--yes"])
    assert rc == 0
    # PATCH to tasks should have been issued
    assert any("/tasks/" in path and method == "PATCH" for method, path, _ in mock_api)
    out = capsys.readouterr().out
    # human summary without --json
    assert "Requeued" in out


def test_ops_requeue_failed_json(mock_api, monkeypatch, capsys):
    """ops requeue-failed --json emits JSON output."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["--json", "ops", "requeue-failed", "--yes"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "count" in parsed
    assert "requeued" in parsed


def test_logs_json_with_mocked_fetch(monkeypatch, capsys):
    """logs emits valid JSON; gcloud is mocked so no SDK is required."""
    sample = [{"timestamp": "2026-06-15T00:00:00Z", "severity": "ERROR",
               "textPayload": "boom\nsecond line"}]
    monkeypatch.setattr(blueprint, "_fetch_logs", lambda f, lim: sample)
    rc = blueprint.main(["--json", "logs", "errors", "--minutes", "5"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["subcommand"] == "errors"
    assert parsed["entries"][0]["severity"] == "ERROR"
    assert parsed["entries"][0]["text"].startswith("boom")


def test_logs_graceful_when_gcloud_missing(monkeypatch, capsys):
    """Missing gcloud degrades to an empty result, not a crash."""
    monkeypatch.setattr(blueprint, "_resolve_gcloud", lambda: None)
    rc = blueprint.main(["logs", "errors"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "gcloud not found" in err


def test_tasks_scope_json(mock_api, capsys):
    """tasks scope --json returns id, prompt, and metadata for a task."""
    rc = blueprint.main(["--json", "tasks", "scope", "task-123"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["id"] == "task-123"
    # prompt key is present (may be empty string if fixture has none)
    assert "prompt" in parsed
    assert "prompt_length" in parsed


def test_tasks_scope_human_render(mock_api, capsys):
    """tasks scope human output includes the task id and status."""
    rc = blueprint.main(["tasks", "scope", "task-123"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "task-123"[:8] in out
    assert "IN_PROGRESS" in out


def test_tasks_scope_verbose_flag_accepted(mock_api, capsys):
    """-v flag is accepted without error."""
    rc = blueprint.main(["tasks", "scope", "task-123", "-v"])
    assert rc == 0


def _make_tree_api(monkeypatch):
    """Return a mock_api fixture that includes child_task_ids for tree tests."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if "/tasks/child-456" in path:
            return {
                "id": "child-456",
                "title": "child task",
                "task_type": "create",
                "status": "COMPLETED",
                "created_at": "2026-06-15T00:00:00Z",
                "payloads": [{"type": "create", "pr_number": 99}],
                "context": {"pr_merged": True},
                "child_task_ids": [],
            }
        if "/tasks/task-123" in path:
            return {
                "id": "task-123",
                "title": "parent task",
                "task_type": "research",
                "status": "EVALUATING",
                "created_at": "2026-06-15T00:00:00Z",
                "payloads": [{"type": "research", "prompt": "do research"}],
                "context": {},
                "child_task_ids": ["child-456"],
            }
        if "/tasks" in path:
            return {"tasks": [], "total": 0}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    return calls


def test_tasks_tree_json(monkeypatch, capsys):
    """tasks tree --json renders parent plus children with status/PR/merge state."""
    calls = _make_tree_api(monkeypatch)
    rc = blueprint.main(["--json", "tasks", "tree", "task-123"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    # parent is present
    assert parsed["parent"]["id"] == "task-123"
    assert parsed["parent"]["status"] == "EVALUATING"
    # child is present with merge state
    assert len(parsed["children"]) == 1
    child = parsed["children"][0]
    assert child["id"] == "child-456"
    assert child["status"] == "COMPLETED"
    assert child["context"]["pr_merged"] is True
    # both parent and child were fetched
    fetched_paths = [p for _, p, _ in calls if "/tasks/" in p]
    assert any("task-123" in p for p in fetched_paths)
    assert any("child-456" in p for p in fetched_paths)


def test_tasks_tree_human_render(monkeypatch, capsys):
    """Human-readable tree output includes status icon and merge state."""
    _make_tree_api(monkeypatch)
    rc = blueprint.main(["tasks", "tree", "task-123"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TREE" in out
    assert "merged=" in out
    # child is rendered (COMPLETED → icon D)
    assert "[D]" in out


def test_tasks_tree_no_children(monkeypatch, capsys):
    """tree renders a no-children message when child_task_ids is empty."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        return {
            "id": "task-123", "title": "parent", "task_type": "research",
            "status": "IN_PROGRESS", "payloads": [], "context": {},
            "child_task_ids": [],
        }

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "tree", "task-123"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no children" in out


def test_tasks_list_http_404_is_clean(monkeypatch, capsys):
    """Non-200 API responses print a clean error to stderr and return 1, no traceback."""
    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(
            url="http://example.com/api/admin/tasks",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b"tasks not found"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "http://example.com", "api_key": "test-key"})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "list"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "404" in err
    assert "Traceback" not in err


def test_api_401_prints_credential_guidance(monkeypatch, capsys):
    """401 responses print credential-specific guidance before the generic error line."""
    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(
            url="http://example.com/api/admin/tasks",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b"unauthorized"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "http://example.com", "api_key": "test-key"})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "list"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "401" in err
    assert "expired" in err


def test_network_error_is_clean(monkeypatch, capsys):
    """Network failures print a clean 'cannot reach' message and return 1."""
    def fake_urlopen(req, timeout=30):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "http://example.com", "api_key": "test-key"})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "list"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot reach" in err
    assert "Traceback" not in err


def test_api_403_read_only_prints_guidance(monkeypatch, capsys):
    """403 responses with a read-only config print specific admin-credential guidance."""
    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(
            url="http://example.com/api/admin/tasks",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b"forbidden"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "http://example.com", "api_key": "agent-token", "read_only": True
    })
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "list"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "403" in err
    assert "read-only" in err


def test_status_degrades_gracefully_on_real_http_error(monkeypatch, capsys):
    """_safe_read catches APIError from real api() so status degrades gracefully on HTTP errors."""
    call_count = [0]

    def fake_urlopen(req, timeout=30):
        call_count[0] += 1
        if "/admin/state" in req.full_url:
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=500,
                msg="Internal Server Error",
                hdrs={},
                fp=io.BytesIO(b"server error"),
            )
        buf = io.BytesIO(b"{}")
        buf.__enter__ = lambda s: s
        buf.__exit__ = lambda s, *a: None
        return buf

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "http://example.com", "api_key": "test-key"})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["--json", "status"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "state" in parsed["errors"]
    assert "500" in parsed["errors"]["state"]


def test_versions_show_current_includes_changelog(mock_api, capsys):
    """versions show for the open version renders the changelog grouped by task type."""
    rc = blueprint.main(["versions", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Changelog" in out
    assert "[create]" in out
    assert "[fix]" in out
    assert "Add widget feature" in out
    assert "#101" in out
    assert "Fix null pointer" in out
    # serving status should appear
    assert "SERVING" in out
    # no extra API calls for the current version
    assert not any("release-versions" in p for _, p, _ in mock_api)


def test_versions_show_recent_calls_summary_endpoint(mock_api, capsys):
    """versions show N for a closed version fetches the summary endpoint and renders changelog."""
    rc = blueprint.main(["versions", "show", "11"])
    assert rc == 0
    out = capsys.readouterr().out
    # summary endpoint must have been called
    assert any("release-versions/uuid-v11/summary" in p for _, p, _ in mock_api)
    # changelog from summary response should be rendered
    assert "Changelog" in out
    assert "[create]" in out
    assert "[refactor]" in out
    assert "Shipped feature A" in out
    assert "#90" in out
    # headline from highlights
    assert "10 tasks merged" in out
    # serving status
    assert "SERVING" in out


def test_versions_show_json_unchanged(mock_api, capsys):
    """--json for versions show still returns the base summary fields unchanged."""
    rc = blueprint.main(["--json", "versions", "show"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["version_number"] == 12
    assert parsed["phase"] == "build"
    # tasks_merged and aggregate_serving_status are present in JSON
    assert "tasks_merged" in parsed
    assert len(parsed["tasks_merged"]) == 3


def test_versions_list_shows_task_count(mock_api, capsys):
    """versions list shows tasks=N for the current version when tasks_merged is populated."""
    rc = blueprint.main(["versions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tasks=3" in out


def test_versions_list_shows_headline(mock_api, capsys):
    """versions list renders the human-readable phase-context headline per row."""
    rc = blueprint.main(["versions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    # Current (open) version: phase build, 3 merged tasks.
    assert "open in build with 3 merged" in out
    # Recent promoted version (no serving signal) -> plain "promoted".
    assert "promoted" in out


def test_versions_list_uses_server_headline_for_past_versions(mock_api, capsys):
    """versions list shows the server-supplied highlights.headline (not a local
    re-derivation) for past versions, and shows a merged count."""
    rc = blueprint.main(["versions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    # The mock recent_versions entry has highlights.headline = "promoted & serving"
    # and merged_count / merged_task_count = 10.
    assert "promoted & serving" in out
    # merged count from the row
    assert "merged=10" in out


def test_versions_list_default_uses_lifecycle_not_paginated(mock_api, capsys):
    """Default `versions list` keeps using the cached lifecycle endpoint and
    never touches the paginated release-versions endpoint."""
    rc = blueprint.main(["versions", "list"])
    assert rc == 0
    paths = [p for _, p, _ in mock_api]
    assert any("version-lifecycle" in p for p in paths)
    assert not any("release-versions?" in p for p in paths)


def test_versions_list_all_browses_full_history(mock_api, capsys):
    """`versions list --all` hits the paginated endpoint, shows the total in the
    header, and renders every version newest-first."""
    rc = blueprint.main(["versions", "list", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== RELEASE VERSIONS (4 total) ===" in out
    # All four versions rendered.
    for n in (1, 2, 3, 4):
        assert f"v{n}" in out
    # Window footer covers the whole set.
    assert "(showing 0-3 of 4)" in out
    # The paginated endpoint was used; lifecycle was NOT.
    paths = [p for _, p, _ in mock_api]
    assert any("release-versions?" in p for p in paths)
    assert not any("version-lifecycle" in p for p in paths)


def test_versions_list_all_json_envelope(mock_api, capsys):
    """`--json versions list --all` emits the full envelope with total/limit/offset."""
    rc = blueprint.main(["--json", "versions", "list", "--all"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["total"] == 4
    assert len(parsed["versions"]) == 4
    assert [v["version_number"] for v in parsed["versions"]] == [4, 3, 2, 1]


def test_versions_list_limit_offset_window(mock_api, capsys):
    """`versions list --limit N --offset M` requests an explicit window and shows it."""
    rc = blueprint.main(["versions", "list", "--limit", "2", "--offset", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(showing 1-2 of 4)" in out
    # limit/offset are inlined into the request path.
    assert any("limit=2&offset=1" in p for _, p, _ in mock_api)


def test_version_headline_derivation():
    """_version_headline mirrors the backend summary headline for each state."""
    assert (
        blueprint._version_headline(
            {"status": "promoted", "aggregate_serving_status": "serving_confirmed"}
        )
        == "promoted & serving"
    )
    # Lifecycle endpoint may use the upper-case "SERVING" vocabulary.
    assert (
        blueprint._version_headline({"status": "promoted", "serving_status": "SERVING"})
        == "promoted & serving"
    )
    assert blueprint._version_headline({"status": "promoted"}) == "promoted"
    assert blueprint._version_headline({"status": "abandoned"}) == "abandoned"
    assert (
        blueprint._version_headline({"phase": "stabilize", "merged_task_count": 0})
        == "open in stabilize with no merged work yet"
    )
    assert (
        blueprint._version_headline({"phase": "build", "merged_count": 7})
        == "open in build with 7 merged"
    )


def test_versions_show_recent_no_id_graceful(monkeypatch, capsys):
    """versions show N for a closed version without an id skips the summary call gracefully."""
    def fake_api(method, path, body=None, timeout=30):
        if "version-lifecycle" in path:
            return {
                "current_version": {"version_number": 12, "phase": "build"},
                "recent_versions": [
                    {"version_number": 11, "phase": "stabilize", "status": "promoted",
                     "merged_task_count": 5},
                ],
                "promotion_enabled_effective": True,
            }
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["versions", "show", "11"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VERSION v11" in out
    # no crash; empty changelog is silently omitted
    assert "Changelog" not in out


def test_tasks_cancel_requeue_guards(mock_api, monkeypatch, capsys):
    """tasks cancel and tasks requeue each send the correct PATCH status and honor
    the confirmation + read-only guards."""
    # read-only guard refuses cancel
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "cancel", "task-123", "--yes"])
    assert exc.value.code == 1

    # read-only guard refuses requeue
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "requeue", "task-123", "--yes"])
    assert exc.value.code == 1

    # with admin key, cancel sends PATCH {"status": "CANCELLED"}
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    mock_api.clear()
    blueprint.main(["tasks", "cancel", "task-123", "--yes"])
    assert any(
        m == "PATCH" and "/tasks/task-123" in p and b == {"status": "CANCELLED"}
        for m, p, b in mock_api
    )

    # requeue sends PATCH {"status": "QUEUED"}
    mock_api.clear()
    blueprint.main(["tasks", "requeue", "task-123", "--yes"])
    assert any(
        m == "PATCH" and "/tasks/task-123" in p and b == {"status": "QUEUED"}
        for m, p, b in mock_api
    )


def test_tasks_failures_json(mock_api, capsys):
    """tasks failures --json groups by cause bucket with correct counts."""
    rc = blueprint.main(["--json", "tasks", "failures"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["total"] == 3
    buckets = parsed["buckets"]
    # Two tasks share API_EXHAUSTED cause, one is SPAWN_E2BIG
    assert "API_EXHAUSTED" in buckets
    assert len(buckets["API_EXHAUSTED"]) == 2
    assert "SPAWN_E2BIG" in buckets
    assert len(buckets["SPAWN_E2BIG"]) == 1
    # Each task entry has expected fields
    task_entry = buckets["API_EXHAUSTED"][0]
    assert "id" in task_entry
    assert "title" in task_entry
    assert "attempt_count" in task_entry
    # the API call must include status=FAILED
    assert any("status=FAILED" in path for _, path, _ in mock_api)


def test_tasks_failures_human_render(mock_api, capsys):
    """Human-readable failures output includes cause bucket headers and task ids."""
    rc = blueprint.main(["tasks", "failures"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "FAILED TASKS" in out
    assert "API_EXHAUSTED" in out
    assert "SPAWN_E2BIG" in out
    assert "SUMMARY" in out


def test_tasks_failures_empty(monkeypatch, capsys):
    """tasks failures with no failed tasks emits a graceful message."""
    def fake_api(method, path, body=None, timeout=30):
        return {"tasks": [], "total": 0}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "failures"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No failed tasks." in out


# ── help text and output formatting polish ────────────────────────────────────

def test_tasks_help_has_description():
    parser = blueprint.build_parser()
    tasks_parser = next(
        p for p in parser._subparsers._group_actions[0].choices.values()
        if p.prog == "blueprint tasks"
    )
    assert tasks_parser.description is not None
    assert "list" in tasks_parser.description.lower()


def test_versions_help_has_description():
    parser = blueprint.build_parser()
    versions_parser = next(
        p for p in parser._subparsers._group_actions[0].choices.values()
        if p.prog == "blueprint versions"
    )
    assert versions_parser.description is not None
    assert "version" in versions_parser.description.lower()


def test_logs_no_subcommand_message(capsys):
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["logs"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "--help" in captured.err


def test_top_level_help_has_auth_epilog():
    parser = blueprint.build_parser()
    assert parser.epilog is not None
    assert "BLUEPRINT_API_KEY" in parser.epilog


# ── versions export-changelog ─────────────────────────────────────────────────

def test_versions_export_changelog_stdout(mock_api, capsys):
    """versions export-changelog emits grouped markdown headers and PR links to stdout."""
    rc = blueprint.main(["versions", "export-changelog"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## What shipped in v12" in out
    # tasks_merged has create and fix types
    assert "### create" in out
    assert "### fix" in out
    assert "#101" in out
    assert "Add widget feature" in out
    assert "#102" in out
    assert "Fix null pointer" in out


def test_versions_export_changelog_historical(mock_api, capsys):
    """versions export-changelog N fetches the named version's summary and renders markdown."""
    rc = blueprint.main(["versions", "export-changelog", "11"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## What shipped in v11" in out
    assert "### create" in out
    assert "#90" in out
    assert "Shipped feature A" in out
    assert "### refactor" in out
    assert "#91" in out
    # summary endpoint must have been called for v11
    assert any("release-versions/uuid-v11/summary" in p for _, p, _ in mock_api)


def test_versions_export_changelog_to_file(mock_api, capsys, tmp_path):
    """versions export-changelog --out FILE writes markdown to the given path."""
    out_file = tmp_path / "changelog.md"
    rc = blueprint.main(["versions", "export-changelog", "--out", str(out_file)])
    assert rc == 0
    content = out_file.read_text(encoding="utf-8")
    assert "## What shipped in v12" in content
    assert "### create" in content
    assert "#101" in content
    # stdout should only have the "Wrote changelog to..." notice
    out = capsys.readouterr().out
    assert str(out_file) in out


def test_versions_export_changelog_empty_version(mock_api, monkeypatch, capsys):
    """versions export-changelog with no tasks_merged emits the no-tasks message."""
    def fake_api(method, path, body=None, timeout=30):
        if "version-lifecycle" in path:
            return {
                "current_version": {
                    "version_number": 99, "phase": "build",
                    "tasks_merged": [],
                },
                "recent_versions": [],
            }
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["versions", "export-changelog"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No tasks merged" in out


def test_changelog_to_markdown_pure():
    """_changelog_to_markdown groups by type and formats bullet lines correctly."""
    tasks = [
        {"task_type": "create", "title": "Add widget feature", "pr_number": 101},
        {"task_type": "fix", "title": "Fix null pointer", "pr_number": 102},
        {"task_type": "create", "title": "New dashboard", "pr_number": 103},
    ]
    md = blueprint._changelog_to_markdown(tasks, heading="What shipped in v42")
    assert "## What shipped in v42" in md
    # create section has 2 entries
    assert "### create (2)" in md
    assert "- #101 Add widget feature" in md
    assert "- #103 New dashboard" in md
    # fix section has 1 entry
    assert "### fix (1)" in md
    assert "- #102 Fix null pointer" in md


def test_changelog_to_markdown_no_pr():
    """_changelog_to_markdown omits PR prefix when pr_number is None."""
    tasks = [{"task_type": "other", "title": "Internal cleanup", "pr_number": None}]
    md = blueprint._changelog_to_markdown(tasks)
    assert "- Internal cleanup" in md
    assert "- #" not in md


# ── power-user task subcommands ───────────────────────────────────────────────

def test_tasks_evaluating_json(mock_api, capsys):
    """tasks evaluating --json returns EVALUATING tasks with CI status."""
    rc = blueprint.main(["--json", "tasks", "evaluating"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["total"] == 2
    tasks = parsed["tasks"]
    # first task has ci_waiting=True in context
    ci_task = next(t for t in tasks if t["id"].startswith("eval-aaa"))
    assert ci_task["context"]["review_loop"]["ci_waiting"] is True
    assert any("status=EVALUATING" in path for _, path, _ in mock_api)


def test_tasks_evaluating_human_render(mock_api, capsys):
    """Human-readable evaluating output includes task ids and CI flag."""
    rc = blueprint.main(["tasks", "evaluating"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "EVALUATING TASKS" in out
    assert "ci=True" in out
    assert "pr=42" in out


def test_tasks_evaluating_empty(monkeypatch, capsys):
    """tasks evaluating with no EVALUATING tasks emits a graceful none message."""
    def fake_api(method, path, body=None, timeout=30):
        return {"tasks": [], "total": 0}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "evaluating"])
    assert rc == 0
    assert "(none)" in capsys.readouterr().out


def test_tasks_cancel_all_read_only_refuses(mock_api, monkeypatch):
    """cancel-all refuses under a read-only (agent-JWT) config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "cancel-all", "--yes"])
    assert exc.value.code == 1


def test_tasks_cancel_all_aborts_without_yes(mock_api, monkeypatch, capsys):
    """cancel-all without --yes on non-interactive stdin aborts cleanly."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["tasks", "cancel-all"])
    assert rc == 0
    # no PATCH calls should have been made
    assert not any(m == "PATCH" for m, _, _ in mock_api)


def test_tasks_cancel_all_with_yes_cancels_tasks(mock_api, monkeypatch, capsys):
    """cancel-all --status QUEUED --yes pages through tasks and issues PATCH CANCELLED."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["tasks", "cancel-all", "--status", "QUEUED", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cancelled" in out
    # a PATCH to CANCELLED was issued for the queued task
    assert any(
        m == "PATCH" and "tasks" in p and (b or {}).get("status") == "CANCELLED"
        for m, p, b in mock_api
    )


def test_tasks_set_status_read_only_refuses(mock_api, monkeypatch):
    """set-status refuses under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "set-status", "task-123", "FAILED", "--yes"])
    assert exc.value.code == 1


def test_tasks_set_status_with_yes_issues_patch(mock_api, monkeypatch, capsys):
    """set-status --yes issues a PATCH with the target status."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["tasks", "set-status", "task-123", "FAILED", "--yes"])
    assert rc == 0
    assert any(
        m == "PATCH" and "/tasks/task-123" in p and (b or {}).get("status") == "FAILED"
        for m, p, b in mock_api
    )


def test_tasks_set_status_with_message(mock_api, monkeypatch, capsys):
    """set-status --message records the message in the PATCH body."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main([
        "tasks", "set-status", "task-123", "COMPLETED",
        "--message", "forced done by operator", "--yes",
    ])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in mock_api
                   if m == "PATCH" and "/tasks/task-123" in p]
    assert patch_calls
    body = patch_calls[0][2] or {}
    assert body.get("latest_status_message") == "forced done by operator"


def test_tasks_autonomy_debug_json(mock_api, capsys):
    """tasks autonomy-debug --json exposes blocking guards and directions."""
    rc = blueprint.main(["--json", "tasks", "autonomy-debug"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["spawn_blocked"] is True
    assert len(parsed["blocking_guards"]) == 1
    assert parsed["blocking_guards"][0]["guard"] == "rate_limit"
    assert len(parsed["directions"]) == 2
    assert any("/autonomy-debug" in path for _, path, _ in mock_api)


def test_tasks_autonomy_debug_human_render(mock_api, capsys):
    """Human-readable autonomy-debug output shows blocking guard and direction."""
    rc = blueprint.main(["tasks", "autonomy-debug"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTONOMY DEBUG" in out
    assert "can_spawn: NO" in out
    assert "rate_limit" in out
    assert "eligible" in out


# ── system subcommand group ───────────────────────────────────────────────────

def test_system_zones_json(monkeypatch, capsys):
    """system zones --json surfaces zone health from GET /api/admin/zone-health."""
    def fake_api(method, path, body=None, timeout=30):
        if "zone-health" in path:
            return {
                "us-central1-a": {"status": "OK", "consecutive_failures": 0, "last_success": "2026-06-17T00:00:00Z"},
                "us-east1-b": {"status": "DEGRADED", "consecutive_failures": 2, "last_success": "2026-06-16T00:00:00Z"},
            }
        return {}

    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["--json", "system", "zones"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "zones" in parsed
    assert "us-central1-a" in parsed["zones"]
    assert parsed["zones"]["us-central1-a"]["status"] == "OK"
    assert "us-east1-b" in parsed["zones"]
    assert parsed["zones"]["us-east1-b"]["consecutive_failures"] == 2
    assert parsed.get("error") is None


def test_system_zones_human_render(monkeypatch, capsys):
    """system zones human output includes zone names and status."""
    def fake_api(method, path, body=None, timeout=30):
        if "zone-health" in path:
            return {"us-central1-a": {"status": "OK", "consecutive_failures": 0}}
        return {}

    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["system", "zones"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ZONE HEALTH" in out
    assert "us-central1-a" in out
    assert "OK" in out


# ── fleet command group ───────────────────────────────────────────────────────

def test_fleet_pool_human(mock_api, capsys):
    """fleet pool shows MAX PLAN POOL section with account names."""
    rc = blueprint.main(["fleet", "pool"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "MAX PLAN POOL" in out
    assert "henry" in out
    assert "evan" in out
    assert any("/admin/settings" in path for _, path, _ in mock_api)


def test_fleet_pool_json(mock_api, capsys):
    """fleet pool --json emits a list of accounts under the 'accounts' key."""
    rc = blueprint.main(["--json", "fleet", "pool"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed["accounts"], list)
    assert len(parsed["accounts"]) == 2
    assert parsed["accounts"][0]["name"] == "henry"


def test_fleet_warm_human(mock_api, capsys):
    """fleet warm shows WARM VM POOL and DISPATCH FUNNEL sections."""
    rc = blueprint.main(["fleet", "warm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARM VM POOL" in out
    assert "DISPATCH FUNNEL" in out
    assert "COST GUARD" in out
    assert "CLAIM CIRCUIT BREAKER" in out
    assert any("/warm-vm-state" in path for _, path, _ in mock_api)


def test_fleet_warm_json(mock_api, capsys):
    """fleet warm --json emits the raw warm-vm-state payload."""
    rc = blueprint.main(["--json", "fleet", "warm"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "pool_metrics" in parsed
    assert parsed["pool_metrics"]["enabled"] is True
    assert "warm_pin_metrics" in parsed


def test_fleet_accounts_show(mock_api, capsys):
    """fleet accounts (no flags) defaults to showing the pool."""
    rc = blueprint.main(["fleet", "accounts"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "MAX PLAN POOL" in out
    assert "henry" in out


def test_fleet_accounts_disable_yes(mock_api, monkeypatch, capsys):
    """fleet accounts --disable NAME --yes sends PATCH with enabled=False."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["fleet", "accounts", "--disable", "evan", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Disabled" in out
    patch_calls = [(m, p, b) for m, p, b in mock_api
                   if m == "PATCH" and "max-plan-accounts" in p]
    assert patch_calls, "Expected a PATCH to /max-plan-accounts endpoint"
    assert patch_calls[0][2] == {"enabled": False}


def test_fleet_accounts_enable_yes(mock_api, monkeypatch, capsys):
    """fleet accounts --enable NAME --yes sends PATCH with enabled=True."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["fleet", "accounts", "--enable", "evan", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Enabled" in out
    patch_calls = [(m, p, b) for m, p, b in mock_api
                   if m == "PATCH" and "max-plan-accounts" in p]
    assert patch_calls
    assert patch_calls[0][2] == {"enabled": True}


def test_fleet_accounts_read_only_refused(mock_api, monkeypatch):
    """fleet accounts --disable refuses under read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["fleet", "accounts", "--disable", "evan", "--yes"])
    assert exc.value.code == 1


def test_fleet_accounts_aborts_without_yes(mock_api, monkeypatch, capsys):
    """fleet accounts --disable without --yes on non-interactive stdin aborts."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["fleet", "accounts", "--disable", "evan"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aborted" in out
    assert not any("max-plan-accounts" in p and m == "PATCH" for m, p, _ in mock_api)


def test_fleet_zones_human(mock_api, capsys):
    """fleet zones shows ZONE HEALTH section with per-zone status."""
    rc = blueprint.main(["fleet", "zones"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ZONE HEALTH" in out
    assert "us-central1-a" in out
    assert "us-east1-b" in out
    assert any("/zone-health" in path for _, path, _ in mock_api)


def test_fleet_zones_json(mock_api, capsys):
    """fleet zones --json emits the zones dict under the 'zones' key."""
    rc = blueprint.main(["--json", "fleet", "zones"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "zones" in parsed
    assert "us-central1-a" in parsed["zones"]
    assert parsed["zones"]["us-east1-b"]["consecutive_failures"] == 2

def test_system_autonomy_json(monkeypatch, capsys):
    """system autonomy --json surfaces per-project autonomy state."""
    def fake_api(method, path, body=None, timeout=30):
        if "autonomous-status" in path:
            return {
                "projects": [
                    {"project_name": "proj-alpha", "autonomous_enabled": True,
                     "consecutive_failures": 0, "last_research_at": "2026-06-17T00:00:00Z"},
                ]
            }
        return {}

    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["--json", "system", "autonomy"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "projects" in parsed
    assert len(parsed["projects"]) == 1
    assert parsed["projects"][0]["project_name"] == "proj-alpha"
    assert parsed["projects"][0]["autonomous_enabled"] is True


def test_system_autonomy_human_render(monkeypatch, capsys):
    """system autonomy human output lists project names and enabled state."""
    def fake_api(method, path, body=None, timeout=30):
        if "autonomous-status" in path:
            return {"projects": [{"project_name": "proj-alpha", "autonomous_enabled": True,
                                  "consecutive_failures": 1, "last_research_at": "never"}]}
        return {}

    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["system", "autonomy"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTONOMY" in out
    assert "proj-alpha" in out
    assert "enabled=True" in out


def test_system_repair_read_only_refuses(monkeypatch, capsys):
    """system repair refuses under a read-only (agent-JWT) config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["system", "repair", "--yes"])
    assert exc.value.code == 1


def test_system_repair_fires_with_yes(monkeypatch, capsys):
    """system repair --yes posts to repair-state and renders the result."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path))
        return {"count": 1, "repairs": ["orphaned parent re-linked"]}

    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "api", fake_api)
    rc = blueprint.main(["system", "repair", "--yes"])
    assert rc == 0
    assert any(m == "POST" and "repair-state" in p for m, p in calls)
    out = capsys.readouterr().out
    assert "Repairs: 1" in out
    assert "orphaned parent" in out


def test_system_restart_fires_with_yes(monkeypatch, capsys):
    """system restart --yes posts to restart-orchestrator."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path))
        return {"status": "ok", "loops_restarted": 3}

    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "api", fake_api)
    rc = blueprint.main(["system", "restart", "--yes"])
    assert rc == 0
    assert any(m == "POST" and "restart-orchestrator" in p for m, p in calls)


def test_system_zones_error_path(monkeypatch, capsys):
    """system zones degrades gracefully when the zones endpoint fails."""
    def fail_api(method, path, body=None, timeout=30):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(blueprint, "api", fail_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["--json", "system", "zones"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed.get("error") is not None
    assert "connection refused" in parsed["error"]


def test_system_no_subcommand_exits_nonzero(capsys):
    """system with no subcommand exits 1 and prints a helpful message."""
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["system"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "subcommand required" in err


def test_monitor_alerts_json(mock_api, capsys):
    """monitor alerts --json returns valid JSON with alerts list."""
    rc = blueprint.main(["--json", "monitor", "alerts"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed["alerts"], list)
    assert len(parsed["alerts"]) == 2
    assert parsed["alerts"][0]["severity"] == "WARNING"
    assert parsed["alerts"][0]["title"] == "High memory usage"
    assert parsed["alerts"][1]["severity"] == "ERROR"


def test_monitor_alerts_human_render(mock_api, capsys):
    """monitor alerts human output shows severity, title, and message."""
    rc = blueprint.main(["monitor", "alerts"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALERTS" in out
    assert "WARNING" in out
    assert "High memory usage" in out
    assert "ERROR" in out


def test_monitor_digest_json(mock_api, capsys):
    """monitor digest --json returns valid JSON with digest stats."""
    rc = blueprint.main(["--json", "monitor", "digest"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["hours"] == 24
    assert parsed["digest"]["completed_count"] == 15
    assert parsed["digest"]["failed_count"] == 2
    assert parsed["digest"]["prs_opened"] == 8


def test_monitor_digest_hours_param(mock_api, capsys):
    """monitor digest --hours N passes the hours parameter to the API."""
    rc = blueprint.main(["--json", "monitor", "digest", "--hours", "12"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["hours"] == 12
    # Verify the API was called with the correct hours parameter
    assert any("digest?hours=12" in path for _, path, _ in mock_api)


def test_monitor_activity_json(mock_api, capsys):
    """monitor activity --json returns valid JSON with activity events."""
    rc = blueprint.main(["--json", "monitor", "activity"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed["events"], list)
    assert len(parsed["events"]) == 2
    assert parsed["events"][0]["type"] == "TASK_COMPLETED"
    assert parsed["events"][1]["type"] == "TASK_FAILED"


def test_monitor_activity_limit_param(mock_api, capsys):
    """monitor activity --limit N passes the limit parameter to the API."""
    rc = blueprint.main(["--json", "monitor", "activity", "--limit", "10"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["limit"] == 10
    # Verify the API was called with the correct limit parameter
    assert any("activity?limit=10" in path for _, path, _ in mock_api)


def test_monitor_no_subcommand_exits_nonzero(capsys):
    """monitor with no subcommand exits 1 and prints a helpful message."""
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["monitor"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "subcommand required" in err


# ── directions command group ─────────────────────────────────────────────────

def test_directions_group(mock_api, monkeypatch, capsys):
    """End-to-end coverage for blueprint directions list/show/add/update/unhold."""
    rc = blueprint.main(["directions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DIRECTIONS" in out
    assert "Improve test coverage" in out
    assert "staged→v13" in out
    assert "Stage the next release" in out
    assert "Refactor legacy modules" not in out
    assert any("/directions" in path for _, path, _ in mock_api)
    mock_api.clear()
    rc = blueprint.main(["--json", "directions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "directions" in payload
    assert len(payload["directions"]) >= 2
    assert payload["directions"][0]["id"] == "d-abc12345"
    assert any(d["status"] == "staged" for d in payload["directions"])
    mock_api.clear()
    rc = blueprint.main(["directions", "list", "--staged"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stage the next release" in out
    assert "Improve test coverage" not in out
    assert "Refactor legacy modules" not in out
    mock_api.clear()
    rc = blueprint.main(["directions", "list", "--staged", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stage the next release" in out
    assert "Improve test coverage" not in out
    assert "Refactor legacy modules" not in out
    mock_api.clear()
    rc = blueprint.main(["directions", "show", "d-abc"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "d-abc12345" in out
    assert "Improve test coverage" in out
    assert "DIRECTION:" in out
    mock_api.clear()
    rc = blueprint.main(["directions", "show", "d-staged"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "d-staged12345" in out
    assert "Stage the next release" in out
    assert "Staged for: v13" in out
    assert "Staged at:" in out
    assert "Waiting age:" in out
    mock_api.clear()
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["directions", "unhold", "d-abc12345", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Unheld" in out or "was_held" in out.lower() or "d-abc12345" in out
    unhold_calls = [(m, p) for m, p, _ in mock_api if m == "POST" and "unhold" in p]
    assert unhold_calls, "Expected a POST to /unhold endpoint"
    mock_api.clear()
    rc = blueprint.main(["directions", "update", "d-abc12345", "--priority", "high", "--yes"])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in mock_api if m == "PATCH" and "directions" in p]
    assert patch_calls, "Expected a PATCH to directions endpoint"
    assert patch_calls[0][2].get("priority") == "high"
    mock_api.clear()
    rc = blueprint.main(["directions", "update", "d-staged12345", "--staged-for-version", "14", "--yes"])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in mock_api if m == "PATCH" and "directions" in p]
    assert patch_calls, "Expected a PATCH to directions endpoint"
    assert patch_calls[0][2] == {"staged_for_version": 14}
    mock_api.clear()
    rc = blueprint.main(["directions", "retarget", "d-staged", "15", "--yes"])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in mock_api if m == "PATCH" and "directions" in p]
    assert patch_calls, "Expected retarget to PATCH directions endpoint"
    assert patch_calls[0][1].endswith("/directions/d-staged12345")
    assert patch_calls[0][2] == {"staged_for_version": 15}
    mock_api.clear()
    rc = blueprint.main(["directions", "activate", "d-staged", "--yes"])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in mock_api if m == "PATCH" and "directions" in p]
    assert patch_calls and patch_calls[0][1].endswith("/directions/d-staged12345")
    assert patch_calls[0][2] == {"status": "active"}
    mock_api.clear()
    rc = blueprint.main(["directions", "cancel", "d-staged", "--yes"])
    assert rc == 0
    delete_calls = [(m, p) for m, p, _ in mock_api if m == "DELETE" and "directions" in p]
    assert delete_calls, "Expected cancel to DELETE directions endpoint"
    assert delete_calls[0][1].endswith("/directions/d-staged12345")
    mock_api.clear()
    rc = blueprint.main(["directions", "unhold", "d-abc", "--yes"])
    assert rc == 0
    unhold_calls = [(m, p) for m, p, _ in mock_api if m == "POST" and "unhold" in p]
    assert unhold_calls, "Expected unhold to POST to /unhold endpoint"
    assert unhold_calls[0][1].endswith("/directions/d-abc12345/unhold")
    mock_api.clear()
    rc = blueprint.main(["directions", "add", "some staged text", "--staged", "--yes"])
    assert rc == 0
    post_calls = [(m, p, b) for m, p, b in mock_api if m == "POST" and "/directions" in p]
    assert post_calls and post_calls[0][2].get("staged") is True
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    for argv in [
        ["directions", "add", "some text", "--yes"],
        ["directions", "add", "some text", "--staged", "--yes"],
        ["directions", "update", "d-abc", "--priority", "low", "--yes"],
        ["directions", "update", "d-staged12345", "--staged-for-version", "16", "--yes"],
        ["directions", "retarget", "d-staged12345", "16", "--yes"],
        ["directions", "activate", "d-staged12345", "--yes"],
        ["directions", "cancel", "d-staged12345", "--yes"],
        ["directions", "unhold", "d-abc", "--yes"],
    ]:
        with pytest.raises(SystemExit) as exc:
            blueprint.main(argv)
        assert exc.value.code == 1

    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    for argv, expected in [
        (["directions", "retarget", "d-abc", "15", "--yes"], "Direction is not staged"),
        (["directions", "activate", "d-abc", "--yes"], "Direction is not staged"),
        (["directions", "cancel", "d-abc", "--yes"], "Direction is not staged"),
        (["directions", "activate", "d-", "--yes"], "Direction prefix is ambiguous"),
        (["directions", "cancel", "d-nope", "--yes"], "Direction not found"),
    ]:
        mock_api.clear()
        with pytest.raises(SystemExit) as exc:
            blueprint.main(argv)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert expected in err
        assert not any(m in {"PATCH", "DELETE"} and "directions/" in p for m, p, _ in mock_api)


def test_trials_json(mock_api, capsys):
    """trials --json surfaces capability-trial verdict, arm counts, and days-to-decision."""
    rc = blueprint.main(["--json", "trials"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "trials" in parsed
    assert len(parsed["trials"]) == 1

    trial = parsed["trials"][0]
    assert trial["cap_name"] == "repo_map"
    assert trial["verdict"] == "adopt"
    assert trial["lift"] == 1.25

    # Verify arm composition is present
    arm_comp = trial["arm_composition"]
    assert arm_comp["with_assigned"] == 45
    assert arm_comp["without_assigned"] == 38

    # Verify accrual projection includes days-to-decision
    accrual = trial["accrual_projection"]
    assert accrual["days_to_min_n"] == 5.5
    assert accrual["alternative_projections"]["0.3"] == 5.5

    # Verify containment and delivery rates
    assert trial["containment_rate"] == 0.95
    assert trial["delivery_rate"] == 0.98

    # Verify the API was called
    assert any("/admin/digest" in path for _, path, _ in mock_api)


def test_trials_human_render(mock_api, capsys):
    """Human-readable trials output includes verdict, arm counts, and days-to-decision."""
    rc = blueprint.main(["trials"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CAPABILITY TRIALS" in out
    assert "repo_map" in out
    assert "verdict=adopt" in out
    assert "with_assigned=45" in out
    assert "without_assigned=38" in out
    assert "days_to_decision=5.5" in out


def test_trials_empty(monkeypatch, capsys):
    """trials with no capability verdicts emits a graceful message."""
    def fake_api(method, path, body=None, timeout=30):
        if "/admin/digest" in path:
            return {"capability_adoption_verdicts": {}}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["trials"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(none)" in out


def test_settings_group(monkeypatch, capsys):
    """settings show (human + --json) and set (PATCH body, type coercion, read-only guard)."""
    calls = []
    sample = {"max_concurrent_vms": 10, "promotion_enabled": True, "daily_cost_ceiling_usd": 50.0}

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if "/admin/settings" in path:
            return dict(sample) if method == "GET" else {**sample, **(body or {})}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")

    # show --json emits valid JSON with settings fields
    rc = blueprint.main(["--json", "settings", "show"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["max_concurrent_vms"] == 10
    assert parsed["promotion_enabled"] is True

    # show human renders RUNTIME SETTINGS section
    calls.clear()
    rc = blueprint.main(["settings"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RUNTIME SETTINGS" in out
    assert "max_concurrent_vms" in out
    assert any("/admin/settings" in p for _, p, _ in calls)

    # set with read-only config is refused
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["settings", "set", "max_concurrent_vms=5", "--yes"])
    assert exc.value.code == 1

    # set with admin key sends PATCH with type-coerced values
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    calls.clear()
    rc = blueprint.main(["settings", "set", "max_concurrent_vms=5",
                         "promotion_enabled=true", "daily_cost_ceiling_usd=99.5", "--yes"])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in calls if m == "PATCH"]
    assert len(patch_calls) == 1
    _, _, body = patch_calls[0]
    assert body["max_concurrent_vms"] == 5          # int, not "5"
    assert body["promotion_enabled"] is True         # bool, not "true"
    assert body["daily_cost_ceiling_usd"] == 99.5   # float


# ── messages group ────────────────────────────────────────────────────────────

def test_health_group(monkeypatch, capsys):
    """health deploy/autonomy/alerts/digest/activity all produce structured --json output."""
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if "/admin/deploy-state" in path:
            return {
                "status": "RUNNING", "pause_reason": None, "paused_at": None,
                "health_check_interval_seconds": 30,
                "health_recovery_streak": 3, "health_recovery_streak_required": 3,
                "health_recovery_history": [True, True, True, False, True],
                "health_recovery_history_size": 5,
                "health_failure_streak": 0, "health_failure_streak_threshold": 3,
                "external_health": {
                    "github_api": True, "db": True, "anthropic_api": True,
                    "errors": {},
                },
            }
        if "/health/detailed" in path:
            return {
                "components": {
                    "orchestrator": {"status": "ok", "cycle_age_seconds": 5,
                                     "queued_tasks": 2, "active_tasks": 3}
                }
            }
        if "/admin/autonomous-status" in path:
            return {
                "projects": [{
                    "project_name": "blueprint", "autonomous_enabled": True,
                    "consecutive_failures": 0, "tasks_spawned_last_hour": 2,
                    "last_research_at": "2026-06-17T10:00:00Z",
                    "current_direction": {"summary": "Improve test coverage"},
                }]
            }
        if "/admin/alerts" in path and method == "GET":
            return [
                {"id": "alert-001", "severity": "WARNING", "title": "High error rate",
                 "message": "error rate above threshold", "created_at": "2026-06-17T10:00:00Z",
                 "task_id": None},
            ]
        if "/admin/digest" in path:
            return {
                "completed_count": 5, "failed_count": 1, "prs_opened": 3,
                "research_cycles": 2, "pending_approval_count": 0,
                "active_count": 4, "running_count": 3, "queued_count": 1,
                "needs_attention": [], "recent_completions": [], "recent_failures": [],
            }
        if "/admin/activity" in path:
            return {
                "events": [
                    {"type": "TASK_COMPLETED", "timestamp": "2026-06-17T10:00:00Z",
                     "title": "Add widget feature", "task_id": "task-abc",
                     "pr_url": "https://github.com/x/y/pull/42",
                     "duration_seconds": 120, "error_summary": None},
                ],
                "total": 1,
            }
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})

    # health deploy --json
    calls.clear()
    rc = blueprint.main(["--json", "health", "deploy"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "RUNNING"
    assert parsed["orchestrator"]["status"] == "ok"
    assert parsed["orchestrator"]["cycle_age_seconds"] == 5
    assert any("/admin/deploy-state" in p for _, p, _ in calls)
    assert any("/health/detailed" in p for _, p, _ in calls)

    # health deploy human render
    calls.clear()
    rc = blueprint.main(["health", "deploy"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEPLOY HEALTH" in out
    assert "RING BUFFER" in out
    assert "ORCHESTRATOR" in out

    # health autonomy --json
    calls.clear()
    rc = blueprint.main(["--json", "health", "autonomy"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed["projects"]) == 1
    assert parsed["projects"][0]["project_name"] == "blueprint"
    assert parsed["projects"][0]["autonomous_enabled"] is True

    # health alerts --json
    calls.clear()
    rc = blueprint.main(["--json", "health", "alerts"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed["alerts"]) == 1
    assert parsed["alerts"][0]["severity"] == "WARNING"

    # health alerts human render
    rc = blueprint.main(["health", "alerts"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALERTS" in out
    assert "High error rate" in out

    # health digest --json
    calls.clear()
    rc = blueprint.main(["--json", "health", "digest", "--hours", "48"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["hours"] == 48
    assert parsed["completed_count"] == 5
    assert parsed["failed_count"] == 1
    assert any("hours=48" in p for _, p, _ in calls)

    # health activity --json
    calls.clear()
    rc = blueprint.main(["--json", "health", "activity", "--limit", "10"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed["events"]) == 1
    assert parsed["events"][0]["type"] == "TASK_COMPLETED"
    assert any("limit=10" in p for _, p, _ in calls)

    # health activity human render
    rc = blueprint.main(["health", "activity"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ACTIVITY" in out
    assert "Add widget feature" in out


def test_health_alerts_dismiss_read_only_refused(monkeypatch, capsys):
    """health alerts --dismiss refuses under read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["health", "alerts", "--dismiss", "alert-001"])
    assert exc.value.code == 1


def test_health_deploy_defaults_no_subcommand(monkeypatch, capsys):
    """health without a subcommand defaults to deploy."""
    def fake_api(method, path, body=None, timeout=30):
        if "/admin/deploy-state" in path:
            return {"status": "RUNNING", "external_health": {}}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["health"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEPLOY HEALTH" in out


def test_ops_health_deprecation(capsys):
    """ops.py health/autonomy/alerts/digest/activity print DEPRECATED and exit 1."""
    import ops
    for cmd in ("cmd_health", "cmd_autonomy", "cmd_alerts", "cmd_digest", "cmd_activity"):
        with pytest.raises(SystemExit) as exc:
            getattr(ops, cmd)([])
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "DEPRECATED" in out


def test_tasks_health_autonomy_debug_deprecation(capsys):
    """tasks.py health and autonomy-debug print DEPRECATED and exit 1."""
    import tasks
    for cmd in ("cmd_health", "cmd_autonomy_debug"):
        with pytest.raises(SystemExit) as exc:
            getattr(tasks, cmd)([])
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "DEPRECATED" in out


def test_messages_group(mock_api, monkeypatch, capsys):
    """messages list (human + --json, status filter) and send (POST body, type
    passthrough, read-only refused) via blueprint.main with mocked api."""

    # --- list human render ---
    rc = blueprint.main(["messages", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[FB]" in out           # FEEDBACK icon
    assert "please fix the CI" in out
    assert "[Q]" in out            # QUESTION icon
    assert "operator" in out
    assert "msg-001" in out        # message ID
    assert "msg-002" in out        # message ID
    # default status=open must be sent
    assert any("/messages?status=open" in path for _, path, _ in mock_api)

    # --- list --json output ---
    mock_api.clear()
    rc = blueprint.main(["--json", "messages", "list"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed["messages"], list)
    assert len(parsed["messages"]) == 2
    assert parsed["messages"][0]["type"] == "FEEDBACK"

    # --- status filter is threaded into the URL ---
    mock_api.clear()
    rc = blueprint.main(["messages", "list", "--status", "all"])
    assert rc == 0
    assert any("/messages?status=all" in path for _, path, _ in mock_api)

    # --- send posts correct body and type ---
    mock_api.clear()
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["messages", "send", "hello world", "--type", "NOTE"])
    assert rc == 0
    post_calls = [(m, p, b) for m, p, b in mock_api if m == "POST" and "/messages" in p]
    assert len(post_calls) == 1
    _, _, body = post_calls[0]
    assert body["content"] == "hello world"
    assert body["message_type"] == "NOTE"
    out = capsys.readouterr().out
    assert "msg-abc" in out

    # --- read-only config refuses send ---
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["messages", "send", "blocked send"])
    assert exc.value.code == 1


def test_messages_answer(mock_api, monkeypatch, capsys):
    """messages answer resolves a QUESTION via PATCH /api/admin/messages/{id}."""

    # --- answer calls PATCH with resolution body ---
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    mock_api.clear()
    rc = blueprint.main(["messages", "answer", "msg-002", "yes, we are stuck"])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in mock_api if m == "PATCH" and "/messages/msg-002" in p]
    assert len(patch_calls) == 1
    _, path, body = patch_calls[0]
    assert "/messages/msg-002" in path
    assert body["resolution"] == "yes, we are stuck"
    # The old /answer POST route must not have been called
    assert not any("/answer" in p for _, p, _ in mock_api)
    out = capsys.readouterr().out
    assert "Answer sent" in out

    # --- answer with --json emits valid JSON with id and status ---
    mock_api.clear()
    rc = blueprint.main(["--json", "messages", "answer", "msg-002", "response text"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["id"] == "msg-002"
    assert parsed["status"] == "resolved"

    # --- read-only config refuses answer ---
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["messages", "answer", "msg-002", "blocked answer"])
    assert exc.value.code == 1


# ── envelope group ────────────────────────────────────────────────────────────

def test_envelope_show(mock_api, monkeypatch, capsys):
    """envelope show (human + --json) and envelope set (PATCH body, type coercion,
    read-only refused) via blueprint.main with mocked api."""

    # show human render includes key fields
    rc = blueprint.main(["envelope", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RESOURCE ENVELOPE" in out
    assert "proj-test-uuid" in out
    assert "max_concurrent_vms" in out
    assert "spend_ceiling_usd" in out
    assert "is_throttled" in out
    assert any("/resource-envelope" in p for _, p, _ in mock_api)

    # show --json emits valid JSON with expected fields
    mock_api.clear()
    rc = blueprint.main(["--json", "envelope", "show"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["max_concurrent_vms"] == 4
    assert parsed["spend_ceiling_usd"] == 50.0
    assert parsed["is_throttled"] is False

    # set with read-only config is refused
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["envelope", "set", "max_concurrent_vms=8", "--yes"])
    assert exc.value.code == 1

    # set with admin key sends PATCH with type-coerced values
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    mock_api.clear()
    rc = blueprint.main(["envelope", "set", "max_concurrent_vms=8",
                         "spend_ceiling_usd=200.0", "--yes"])
    assert rc == 0
    patch_calls = [(m, p, b) for m, p, b in mock_api if m == "PATCH"]
    assert len(patch_calls) == 1
    _, path, body = patch_calls[0]
    assert "/resource-envelope" in path
    assert body["max_concurrent_vms"] == 8          # int, not "8"
    assert body["spend_ceiling_usd"] == 200.0       # float, not "200.0"
    out = capsys.readouterr().out
    assert "RESOURCE ENVELOPE" in out


# ── tasks bulk/manual-state extensions ───────────────────────────────────────

def test_tasks_evaluating_human(mock_api, capsys):
    """tasks evaluating lists EVALUATING tasks with ci/pr state."""
    rc = blueprint.main(["tasks", "evaluating"])
    assert rc == 0
    # mock_api returns a generic task list for /tasks
    assert any("status=EVALUATING" in path for _, path, _ in mock_api)


def test_tasks_evaluating_verbose_flag(mock_api, capsys):
    """-v flag is accepted without error."""
    rc = blueprint.main(["tasks", "evaluating", "-v"])
    assert rc == 0


def test_tasks_cancel_all_read_only_refused(mock_api, monkeypatch):
    """tasks cancel-all refuses under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "cancel-all", "--yes"])
    assert exc.value.code == 1


def test_tasks_requeue_ci_no_ci_tasks(monkeypatch, capsys):
    """tasks requeue-ci with no CI-waiting tasks prints a graceful message."""
    def fake_api(method, path, body=None, timeout=30):
        if "/tasks" in path:
            return {"tasks": [
                {"id": "task-aaa", "task_type": "create", "status": "EVALUATING",
                 "title": "no ci", "context": {"review_loop": {"ci_waiting": False}}}
            ], "total": 1}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["tasks", "requeue-ci"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No CI-waiting" in out


def test_tasks_complete_read_only_refused(mock_api, monkeypatch):
    """tasks complete refuses under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "complete", "task-123", "--yes"])
    assert exc.value.code == 1


def test_tasks_set_status_fires_patch(mock_api, monkeypatch, capsys):
    """tasks set-status sends PATCH with correct status when --yes is passed."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["tasks", "set-status", "task-123", "FAILED", "--yes"])
    assert rc == 0
    assert any(
        m == "PATCH" and "/tasks/task-123" in p and (b or {}).get("status") == "FAILED"
        for m, p, b in mock_api
    )


def test_tasks_archive_read_only_refused(mock_api, monkeypatch):
    """tasks archive refuses under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "archive", "--yes"])
    assert exc.value.code == 1


def test_tasks_archive_fires_bulk_archive(mock_api, monkeypatch, capsys):
    """tasks archive --yes posts to bulk-archive with filter=terminal."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["tasks", "archive", "--yes"])
    assert rc == 0
    assert any(
        m == "POST" and "bulk-archive" in p
        for m, p, _ in mock_api
    )


def test_tasks_research_lists(mock_api, capsys):
    """tasks research lists recent COMPLETED research tasks."""
    rc = blueprint.main(["tasks", "research"])
    assert rc == 0
    assert any("task_type=research" in path for _, path, _ in mock_api)


def test_tasks_reset_iteration_cap_read_only_refused(mock_api, monkeypatch):
    """tasks reset-iteration-cap refuses under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "reset-iteration-cap", "task-123", "--yes"])
    assert exc.value.code == 1


def test_analytics_cost_json(mock_api, capsys):
    """analytics cost --json returns valid JSON with cost breakdown fields."""
    rc = blueprint.main(["--json", "analytics", "cost"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["window"] == "1h"
    assert parsed["task_count"] == 5
    assert isinstance(parsed["task_type_breakdown"], list)
    assert any(r["task_type"] == "create" for r in parsed["task_type_breakdown"])
    assert any("task-cost-breakdown" in path for _, path, _ in mock_api)


def test_analytics_failing_types_json(mock_api, capsys):
    """analytics failing-types --json returns valid JSON with gated_types."""
    rc = blueprint.main(["--json", "analytics", "failing-types"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "gated_types" in parsed
    assert isinstance(parsed["gated_types"], list)
    assert parsed["gated_types"][0]["task_type"] == "create"
    assert parsed["global_failure_threshold"] == 0.8
    assert any("failing-task-types" in path for _, path, _ in mock_api)


def test_envelope_show_json(mock_api, capsys):
    """envelope show --json returns valid JSON with envelope fields."""
    rc = blueprint.main(["--json", "envelope", "show"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["project_id"] == "proj-test-uuid"
    assert parsed["max_concurrent_vms"] == 4
    assert parsed["spend_ceiling_usd"] == 50.0
    assert parsed["is_throttled"] is False
    assert any("resource-envelope" in path for _, path, _ in mock_api)


def test_envelope_set_read_only_refused(mock_api, monkeypatch):
    """envelope set refuses under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["envelope", "set", "max_concurrent_vms=6"])
    assert exc.value.code == 1


def test_envelope_set_patches_envelope(mock_api, monkeypatch, capsys):
    """envelope set --yes PATCHes the resource-envelope endpoint."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["envelope", "set", "--yes", "max_concurrent_vms=6"])
    assert rc == 0
    assert any(
        m == "PATCH" and "resource-envelope" in p
        for m, p, _ in mock_api
    )


# ── knowledge ─────────────────────────────────────────────────────────────────

def test_knowledge_list_applies_filters(mock_api, capsys):
    """knowledge list applies direction and source-kind filters to the API request path."""
    rc = blueprint.main([
        "knowledge", "list",
        "--direction", "d-abc12345",
        "--source-kind", "web_research",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "KNOWLEDGE LIBRARY" in out
    # Both filter params must appear in the request URL using the correct backend param names
    assert any("/knowledge-documents" in path for _, path, _ in mock_api)
    assert any("direction=d-abc12345" in path for _, path, _ in mock_api)
    assert any("source_kind=web_research" in path for _, path, _ in mock_api)
    # The matching document is rendered; the design doc (different direction) is not
    assert "API Rate Limiting" in out
    assert "Design Patterns" not in out


def test_knowledge_list_prints_full_uuid(mock_api, capsys):
    """knowledge list prints the full document ID, not a 12-char prefix."""
    rc = blueprint.main(["knowledge", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "kdoc-001" in out
    assert "kdoc-002" in out


def test_knowledge_show_renders_document_body(mock_api, capsys):
    """knowledge show <id> renders the document title, metadata, and body text."""
    rc = blueprint.main(["knowledge", "show", "kdoc-001"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "KNOWLEDGE" in out
    assert "Research on test coverage" in out
    # body is rendered verbatim
    assert "## Findings" in out
    assert "Test coverage is important for reliability" in out


# ── compute command group ─────────────────────────────────────────────────────

def test_compute_list_json(mock_api, capsys):
    """compute list --json returns compute jobs with status, title, and cost fields."""
    rc = blueprint.main(["--json", "compute", "list"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    jobs = parsed["compute_jobs"]
    assert len(jobs) == 4
    # running job has no cost yet
    running = next(j for j in jobs if j["status"] == "RUNNING")
    assert running["task_title"] == "GPU benchmark alpha"
    assert running["experiment"] == "exp-alpha"
    assert running["final_cost_usd"] is None
    # succeeded jobs have cost and a criterion verdict
    done_jobs = [j for j in jobs if j["status"] == "SUCCEEDED"]
    assert len(done_jobs) == 3
    gpu_benchmark = next(j for j in done_jobs if j["task_title"] == "GPU benchmark beta")
    assert gpu_benchmark["final_cost_usd"] == 12.5
    assert gpu_benchmark["criterion_verdict"]["met"] is True
    assert any("/compute-jobs" in path for _, path, _ in mock_api)


def test_compute_list_human_render(mock_api, capsys):
    """compute list human output shows status and job titles."""
    rc = blueprint.main(["compute", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "COMPUTE JOBS" in out
    assert "RUNNING" in out
    assert "GPU benchmark alpha" in out
    assert "SUCCEEDED" in out
    assert "GPU benchmark beta" in out
    assert "$12.50" in out
    assert "met=True" in out


def test_compute_list_defaults_to_list(mock_api, capsys):
    """compute without a subcommand defaults to list."""
    rc = blueprint.main(["compute"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "COMPUTE JOBS" in out


def test_compute_list_summary_line(mock_api, capsys):
    """compute list human output shows aggregate summary line with total, active, spend, and criteria counts."""
    rc = blueprint.main(["compute", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 jobs" in out
    assert "1 active" in out
    assert "$12.50 total spend" in out
    assert "1/1 criteria met" in out


def test_compute_list_shows_measured_metric_value(mock_api, capsys):
    """compute list shows the measured criterion metric and value for each job that has a verdict."""
    rc = blueprint.main(["compute", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    # SUCCEEDED job has criterion_verdict with metric=latency_ms, value=80.0
    assert "latency_ms=80.0" in out


def test_compute_list_status_filter(mock_api, capsys):
    """compute list --status RUNNING shows only jobs with that status."""
    rc = blueprint.main(["compute", "list", "--status", "RUNNING"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GPU benchmark alpha" in out
    assert "GPU benchmark beta" not in out


def test_compute_list_status_filter_case_insensitive(mock_api, capsys):
    """compute list --status is case-insensitive."""
    rc = blueprint.main(["compute", "list", "--status", "succeeded"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GPU benchmark beta" in out
    assert "GPU benchmark alpha" not in out


def test_compute_kill_read_only_refused(mock_api, monkeypatch):
    """compute kill refuses under a read-only (agent-JWT) config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["compute", "kill", "cjob-aaa0-0000-0000-000000000001", "--yes"])
    assert exc.value.code == 1


def test_compute_kill_with_yes_posts_kill(mock_api, monkeypatch, capsys):
    """compute kill --yes POSTs to the kill endpoint and reports the final status."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    job_id = "cjob-aaa0-0000-0000-000000000001"
    rc = blueprint.main(["compute", "kill", job_id, "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CANCELLED" in out
    kill_calls = [(m, p) for m, p, _ in mock_api if m == "POST" and "/kill" in p]
    assert kill_calls, "Expected a POST to the /kill endpoint"
    assert job_id in kill_calls[0][1]


def test_compute_analyze_with_yes_posts_analyze(mock_api, monkeypatch, capsys):
    """compute analyze --yes POSTs to the analyze endpoint and reports outcome."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    job_id = "cjob-bbb0-0000-0000-000000000002"
    rc = blueprint.main(["compute", "analyze", job_id, "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "success" in out
    analyze_calls = [(m, p) for m, p, _ in mock_api if m == "POST" and "/analyze" in p]
    assert analyze_calls, "Expected a POST to the /analyze endpoint"
    assert job_id in analyze_calls[0][1]


def test_compute_show_renders_result_and_tolerates_missing_fields(mock_api, capsys):
    """compute show <id-prefix> renders metrics, verdict, per-arm results, and verdict margin; missing fields are omitted."""
    # Full-match job id for the SUCCEEDED job in mock_api fixture
    job_id = "cjob-bbb0-0000-0000-000000000002"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    # Core fields must appear
    assert job_id in out
    assert "SUCCEEDED" in out
    assert "GPU benchmark beta" in out
    # Cost rendered
    assert "12.5" in out
    # Criterion verdict rendered with metric, value, threshold, and margin
    assert "met=True" in out
    assert "latency_ms lt 100" in out
    assert "got 80.0" in out
    assert "margin=" in out
    # Hypothesis rendered
    assert "Quantization works" in out
    # result_metrics from polling_metadata rendered (scalar)
    assert "latency_ms" in out
    # Per-arm breakdown rendered
    assert "per-arm results" in out
    assert "arm_a" in out
    assert "arm_b" in out
    # Test prefix-match: short prefix also resolves
    rc2 = blueprint.main(["compute", "show", "cjob-bbb0"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "SUCCEEDED" in out2
    # Fields absent from the RUNNING job (no cost, no verdict) are tolerated
    rc3 = blueprint.main(["compute", "show", "cjob-aaa0"])
    assert rc3 == 0
    out3 = capsys.readouterr().out
    assert "RUNNING" in out3
    assert "GPU benchmark alpha" in out3


def test_compute_show_renders_criterion_and_arms(mock_api, capsys):
    """compute show renders full criterion verdict (expression, value, margin) and per-arm breakdown."""
    job_id = "cjob-bbb0-0000-0000-000000000002"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    # Criterion must show the expression, measured value, and margin
    assert "latency_ms lt 100" in out
    assert "got 80.0" in out
    assert "margin=" in out
    # Per-arm breakdown must print both arms
    assert "[arm_a]" in out
    assert "[arm_b]" in out
    # Scalar metrics also appear (latency_ms=80.0 in scalars dict)
    assert "latency_ms" in out


def test_compute_show_renders_per_arm_transcript(mock_api, capsys):
    """compute show renders per-arm log/label/note transcript readably and renders list-shaped arms."""
    job_id = "cjob-ccc0-0000-0000-000000000003"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    # Command log must appear
    assert "Command log" in out
    assert "Setup phase complete" in out
    assert "Results collected" in out
    # Per-arm breakdown must appear with labels
    assert "baseline" in out
    assert "optimized" in out
    assert "baseline version" in out
    assert "optimized version" in out
    # Per-arm notes must appear
    assert "original implementation" in out
    assert "after refactoring" in out
    # Per-arm logs must appear (rendered as transcripts)
    assert "baseline test output" in out
    assert "optimized test output" in out


def test_compute_show_renders_per_arm_failure_diagnostics(mock_api, capsys):
    """compute show renders a crashed arm's exit code + error excerpt and flags a ran-but-no-metric arm."""
    job_id = "cjob-eee0-0000-0000-000000000005"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    # Per-arm breakdown must appear
    assert "per-arm results" in out
    assert "passing" in out
    assert "crashed" in out
    assert "no_metric" in out
    # Passing arm must show metric
    assert "latency" in out
    assert "75" in out or "75.0" in out
    # Crashed arm must show exit code and error
    assert "exit_code" in out or "exit code" in out
    assert "1" in out
    assert "arm result file not written" in out or "may have failed" in out
    # No-metric arm must show exit code and error
    assert "0" in out
    assert "metric 'latency' not found" in out or "not found in result" in out


def test_compute_show_registers_in_help_invocations():
    """'compute show' is in HELP_INVOCATIONS so --help parsing is verified."""
    assert ["compute", "show"] in HELP_INVOCATIONS


# ── knowledge subcommand tests ────────────────────────────────────────────────

def test_knowledge_list_includes_topic_and_tags(mock_api, capsys):
    """knowledge list output includes topic and tags when present on a document."""
    rc = blueprint.main(["knowledge", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "KNOWLEDGE LIBRARY" in out
    # kdoc-001 has topic=api and tags=[rate-limiting, resilience]
    assert "topic=api" in out
    assert "#rate-limiting" in out
    assert "#resilience" in out
    # kdoc-002 has topic=architecture and empty tags
    assert "topic=architecture" in out


def test_knowledge_list_tag_filter_passed_to_api(mock_api, capsys):
    """knowledge list --tag passes the tag param to the API request path."""
    rc = blueprint.main(["knowledge", "list", "--tag", "rate-limiting"])
    assert rc == 0
    out = capsys.readouterr().out
    # Only kdoc-001 has the rate-limiting tag
    assert "API Rate Limiting" in out
    assert "Design Patterns" not in out
    # The tag filter must appear in the API call path
    assert any("tag=rate-limiting" in path for _, path, _ in mock_api)


def test_knowledge_show_includes_metadata(mock_api, capsys):
    """knowledge show renders summary, topic, tags, and source_citations."""
    rc = blueprint.main(["knowledge", "show", "kdoc-001"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "KNOWLEDGE:" in out
    assert "Topic:" in out
    assert "testing" in out
    assert "Tags:" in out
    assert "#coverage" in out
    assert "#reliability" in out
    assert "Summary:" in out
    assert "Key findings on test coverage" in out
    assert "Citations:" in out
    assert "RFC-001" in out
    assert "https://example.com/plain-url" in out


def test_knowledge_show_resolves_id_prefix(mock_api, capsys):
    """knowledge show resolves an 8-char prefix from the list to the full document."""
    # "kdoc-001" is 8 chars, well under the 36-char UUID threshold
    rc = blueprint.main(["knowledge", "show", "kdoc-001"])
    assert rc == 0
    out = capsys.readouterr().out
    # The detail endpoint was reached and body is rendered
    assert "KNOWLEDGE:" in out
    assert "Research on test coverage" in out
    assert "## Findings" in out
    # The list endpoint must have been called to resolve the prefix
    list_calls = [p for _, p, _ in mock_api if "/knowledge-documents" in p and "limit=200" in p]
    assert list_calls, "Expected a list call with limit=200 for prefix resolution"
    # The detail endpoint must have been called with the resolved full id
    detail_calls = [p for _, p, _ in mock_api if "/knowledge-documents/kdoc-001" in p]
    assert detail_calls, "Expected a detail GET call for /knowledge-documents/kdoc-001"


def test_knowledge_show_prefix_no_match_prints_error(monkeypatch, capsys):
    """knowledge show with an unmatched prefix prints a clear error and returns 0."""
    def fake_api(method, path, body=None, timeout=30):
        if "/knowledge-documents" in path and method == "GET":
            return {"documents": [], "total": 0}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["knowledge", "show", "no-match"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no-match" in err


def test_knowledge_sweep_lists_arms_side_by_side(mock_api, capsys):
    """knowledge sweep <sweep_id> renders a side-by-side arm comparison table."""
    rc = blueprint.main(["knowledge", "sweep", "swp-test001"])
    assert rc == 0
    out = capsys.readouterr().out
    # Header shows the sweep id
    assert "swp-test001" in out
    # Both arm labels appear
    assert "alpha" in out
    assert "beta" in out
    # criterion_met renders distinctly: MET vs NOT MET
    assert "MET" in out
    assert "NOT MET" in out
    # Cost values appear
    assert "1.25" in out
    assert "1.10" in out
    # The API was called with the sweep_id filter
    assert any("sweep_id=swp-test001" in path for _, path, _ in mock_api)


def test_knowledge_sweep_request_carries_sweep_id_and_source_kind(mock_api, capsys):
    """knowledge sweep passes sweep_id and source_kind=compute_experiment to the API."""
    blueprint.main(["knowledge", "sweep", "swp-test001"])
    sweep_calls = [p for _, p, _ in mock_api if "sweep_id=swp-test001" in p]
    assert sweep_calls, "Expected at least one API call with sweep_id=swp-test001"
    assert all("source_kind=compute_experiment" in p for p in sweep_calls)


def test_knowledge_sweep_empty_state(monkeypatch, capsys):
    """knowledge sweep prints a clear message when no arm results are indexed."""
    def fake_api(method, path, body=None, timeout=30):
        if "/knowledge-documents" in path and method == "GET":
            return {"documents": [], "total": 0}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["knowledge", "sweep", "swp-missing"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no arm results" in out.lower() or "none" in out.lower()


def test_knowledge_sweep_aggregate_doc_arms_block(monkeypatch, capsys):
    """knowledge sweep parses arm rows from an aggregate doc's forward_quality_verdict['arms'] block."""
    aggregate_doc = {
        "id": "kdoc-agg-1",
        "title": "Sweep aggregate",
        "source_kind": "compute_experiment",
        "sweep_id": "swp-agg01",
        "arm_label": None,
        "forward_quality_verdict": {
            "arms": {
                "ctrl": {"criterion_met": True, "cost_usd": 0.50, "result_metrics": {"accuracy": 0.95}},
                "trt": {"criterion_met": False, "cost_usd": 0.55, "result_metrics": {"accuracy": 0.78}},
            }
        },
    }

    def fake_api(method, path, body=None, timeout=30):
        if "/knowledge-documents" in path and method == "GET":
            return {"documents": [aggregate_doc], "total": 1}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["knowledge", "sweep", "swp-agg01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ctrl" in out
    assert "trt" in out
    assert "MET" in out
    assert "NOT MET" in out


def test_knowledge_sweep_aggregate_arms_real_criterion_cost(monkeypatch, capsys):
    """Aggregate doc with per_arm_verdicts renders real MET/NOT MET and dollar costs.

    This is the acceptance test for the per-arm criterion+cost fix: the sweep
    table must show real verdict strings and dollar amounts — never '?' — when
    the aggregate document carries a per_arm_verdicts block (populated by the
    backend cost-apportionment path) and an arms block (raw aggregator output).
    """
    aggregate_doc = {
        "id": "kdoc-agg-real",
        "title": "Real sweep aggregate",
        "source_kind": "compute_experiment",
        "sweep_id": "swp-real01",
        "arm_label": None,
        "forward_quality_verdict": {
            "arms": {
                "baseline": {"accuracy": 0.85, "exit_code": 0, "elapsed_seconds": 60.0},
                "optimized": {"accuracy": 0.93, "exit_code": 0, "elapsed_seconds": 40.0},
            },
            "per_arm_verdicts": {
                "baseline": {"value": 0.85, "met": False, "cost_usd": 0.15},
                "optimized": {"value": 0.93, "met": True, "cost_usd": 0.10},
            },
        },
    }

    def fake_api(method, path, body=None, timeout=30):
        if "/knowledge-documents" in path and method == "GET":
            return {"documents": [aggregate_doc], "total": 1}
        return {}

    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["knowledge", "sweep", "swp-real01"])
    assert rc == 0
    out = capsys.readouterr().out

    # Both arm names appear
    assert "baseline" in out
    assert "optimized" in out

    # Real criterion verdicts — no '?' for criterion
    assert "MET" in out
    assert "NOT MET" in out

    # Real costs — dollar amounts appear, no '?' for cost
    assert "$0.15" in out or "0.15" in out
    assert "$0.10" in out or "0.10" in out

    # No '?' in criterion or cost columns (every cell is resolved)
    lines = [ln for ln in out.splitlines() if "baseline" in ln or "optimized" in ln]
    for line in lines:
        # Strip the arm name and metric columns, check criterion+cost are not '?'
        parts = line.split()
        assert "?" not in parts[2:], f"'?' found in criterion/cost column: {line!r}"


def test_compute_kill_aborts_without_yes(mock_api, monkeypatch, capsys):
    """compute kill without --yes on non-interactive stdin aborts cleanly."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["compute", "kill", "cjob-aaa0-0000-0000-000000000001"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aborted" in out
    assert not any(m == "POST" and "/kill" in p for m, p, _ in mock_api)


def test_compute_show_live_cost_telemetry(monkeypatch, capsys):
    """compute show renders burn rate, projected spend, and cap-hit warning for active jobs with cost data."""
    active_job = {
        "id": "cjob-live-0000-0000-000000000099",
        "status": "RUNNING",
        "task_title": "live GPU experiment",
        "task_type": "compute_experiment",
        # started far in the past so elapsed >> 60s in any test run
        "started_at": "2020-01-01T00:00:00Z",
        "accrued_cost_usd": 5.0,
        # cap below accrued so projected spend will always exceed it
        "spend_cap_usd": 3.0,
        "wall_clock_deadline": "2099-01-01T00:00:00Z",
        "final_cost_usd": None,
        "experiment": "exp-live",
        "hypothesis": None,
        "criterion_verdict": None,
    }

    def fake_api(method, path, body=None, timeout=30):
        if method == "GET" and f"/compute-jobs/{active_job['id']}" in path:
            return {"compute_job": active_job}
        return {"compute_jobs": [active_job], "has_more": False}

    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")

    rc = blueprint.main(["compute", "show", "cjob-live"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RUNNING" in out
    # accrued cost shown for active job
    assert "accrued" in out
    assert "5.0000" in out
    # burn rate shown ($/hr)
    assert "burn rate" in out
    assert "/hr" in out
    # projected spend shown
    assert "projected" in out
    # cap-hit warning shown because projected > spend_cap
    assert "cap" in out
    # spend cap displayed
    assert "spend cap" in out
    assert "$3.00" in out
    # deadline displayed
    assert "deadline" in out
    assert "2099" in out


def test_compute_show_resolves_job_beyond_200_cap(mock_api, capsys):
    """compute show resolves a job whose id is not in the list-endpoint results (beyond the 200 cap)."""
    # This job id is served by the detail endpoint mock but NOT in the list response
    job_id = "cjob-old0-0000-0000-000000000201"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert job_id in out
    assert "SUCCEEDED" in out
    assert "Old job beyond 200 cap" in out
    # Verify the detail endpoint was called (not just the list)
    detail_calls = [p for m, p, _ in mock_api if m == "GET" and f"/compute-jobs/{job_id}" in p]
    assert detail_calls, "Expected GET /api/admin/compute-jobs/{id} to be called"


def test_compute_list_burn_rate_column(mock_api, capsys):
    """compute list shows a burn-rate column for an active job with accrued cost and sufficient elapsed time."""
    rc = blueprint.main(["compute", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    # The RUNNING job has started_at=2020-01-01 and accrued_cost_usd=5.0, so burn rate is non-None
    assert "burn=$" in out
    assert "/hr" in out


def test_compute_list_status_filter_footer_reconciled(mock_api, capsys):
    """compute list --status footer counts reflect only the filtered rows, not the server summary."""
    rc = blueprint.main(["compute", "list", "--status", "RUNNING"])
    assert rc == 0
    out = capsys.readouterr().out
    # Only the RUNNING job is shown
    assert "GPU benchmark alpha" in out
    assert "GPU benchmark beta" not in out
    # Footer reflects the 1 filtered job (RUNNING, no cost, no verdict)
    assert "1 jobs" in out
    assert "1 active" in out
    assert "$0.00 total spend" in out
    assert "0/0 criteria met" in out
    # Misleading 'more jobs available' hint suppressed for filtered view
    assert "more jobs available" not in out


def test_compute_show_list_shaped_arms(mock_api, capsys):
    """compute show renders list-shaped per-arm result_metrics with label, note, and log fields."""
    job_id = "cjob-ddd0-0000-0000-000000000004"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "per-arm results" in out
    # Both entries should appear under their labels
    assert "[variant-a]" in out
    assert "[variant-b]" in out
    # label/note/log fields rendered for each entry
    assert "label: variant-a" in out
    assert "label: variant-b" in out
    assert "first variant" in out
    assert "second variant" in out
    assert "variant-a run log" in out
    assert "variant-b run log" in out


def test_compute_show_failed_arm_renders_exit_and_error(mock_api, capsys):
    """compute show renders a failed arm's exit code and error while the sibling arm's metric still appears."""
    job_id = "cjob-ggg0-0000-0000-000000000007"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "per-arm results" in out
    # Healthy arm still shows its metric
    assert "[arm_ok]" in out
    assert "0.91" in out
    # Failed arm must show exit code and error reason
    assert "[arm_crash]" in out
    assert "exit_code: 1" in out
    assert "arm crashed (exit 1): result file not written" in out
    # stderr excerpt must appear
    assert "CUDA OOM" in out


def test_compute_show_list_arms_failed_arm_renders_diagnostics(mock_api, capsys):
    """compute show renders failure diagnostics for a list-shaped arms entry with a timeout."""
    job_id = "cjob-fff0-0000-0000-000000000006"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "per-arm results" in out
    # Healthy arm still shows its metric
    assert "[arm_ok]" in out
    assert "0.85" in out
    # Timed-out arm must show exit code and reason
    assert "[arm_timeout]" in out
    assert "exit_code: 124" in out
    assert "wall-clock limit exceeded" in out
    assert "Timeout after 3600s" in out


def test_compute_show_renders_terminal_reason_and_provisioning_cause(mock_api, capsys):
    """compute show renders terminal_reason and provisioning-failure diagnostics."""
    job_id = "cjob-hhh0-0000-0000-000000000008"
    rc = blueprint.main(["compute", "show", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    # Terminal reason must be displayed
    assert "terminal" in out
    assert "VM provisioning failed" in out
    assert "insufficient quota" in out
    # Provisioning phase must be displayed
    assert "vm_phase" in out
    assert "provisioning" in out
    # Launch contract must be displayed
    assert "deadline" in out
    assert "7200" in out
    assert "model" in out
    assert "SPOT" in out
    # Status and title must be shown
    assert "FAILED" in out
    assert "Provisioning failure test" in out


def test_compute_run_local(capsys):
    """compute run executes a trivial command locally and reports its emitted metric."""
    # Write {"accuracy": 0.95} to the result file that the wrapper provides via $BLUEPRINT_RESULT_FILE
    command = 'python3 -c "import json, os; open(os.environ[\'BLUEPRINT_RESULT_FILE\'], \'w\').write(json.dumps({\'accuracy\': 0.95}))"'
    rc = blueprint.main(["compute", "run", command])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SUCCEEDED" in out
    assert "accuracy" in out
    assert "0.95" in out
    # No API call required — runs entirely locally
    assert "metrics" in out


def test_compute_run_local_reads_spec_and_executes(tmp_path, monkeypatch):
    """compute run --local <spec.json> reads spec, runs command, reports metrics."""
    import subprocess as _subprocess

    spec = {"command": "echo hi", "declared_metric": "loss"}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run(cmd, env=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None
        if rf:
            with open(rf, "w") as f:
                json.dump({"loss": 0.5}, f)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)

    captured = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = captured.getvalue()
    assert "SUCCEEDED" in out
    assert "loss" in out
    assert "0.5" in out


def test_compute_run_no_args_exits(capsys):
    """compute run with no positional and no --local should exit with error."""
    with pytest.raises(SystemExit) as exc_info:
        blueprint.main(["compute", "run"])
    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert "error" in out


def test_compute_run_local_missing_spec_file_exits(tmp_path, capsys):
    """compute run --local with a non-existent spec file exits with error."""
    missing = str(tmp_path / "nonexistent.json")
    with pytest.raises(SystemExit) as exc_info:
        blueprint.main(["compute", "run", "--local", missing])
    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert "error" in out and "could not read spec file" in out


def test_compute_run_local_missing_command_field_exits(tmp_path, capsys):
    """compute run --local with a spec file missing 'command' exits with error."""
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"declared_metric": "loss"}))
    with pytest.raises(SystemExit) as exc_info:
        blueprint.main(["compute", "run", "--local", str(spec_file)])
    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert "error" in out and "command" in out


def test_compute_run_local_not_captured_metric(tmp_path, monkeypatch, capsys):
    """compute run --local prints NOT CAPTURED when declared_metric is absent from emitted metrics."""
    import subprocess as _subprocess

    spec = {"command": "echo hi", "declared_metric": "auc"}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run(cmd, env=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None
        if rf:
            with open(rf, "w") as f:
                json.dump({"loss": 0.5}, f)  # 'auc' not present

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "NOT CAPTURED" in out


# ---------------------------------------------------------------------------
# Success-criterion verdict tests (case-insensitive capture->eval loop)
# ---------------------------------------------------------------------------

def _make_fake_run(metrics: dict | None, returncode: int = 0):
    """Return a fake subprocess.run that writes metrics to BLUEPRINT_RESULT_FILE."""
    def fake_run(cmd, env=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None
        if rf and metrics is not None:
            with open(rf, "w") as f:
                json.dump(metrics, f)

        class R:
            pass
        R.returncode = returncode
        return R()
    return fake_run


def test_compute_run_local_criterion_verdict_case_mismatch(tmp_path, monkeypatch, capsys):
    """Case-insensitive capture->eval loop: emits Accuracy=0.95, criterion accuracy >= 0.9 => MET."""
    import subprocess as _subprocess

    spec = {
        "command": "echo hi",
        "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    # Command emits upper-camel-case key; criterion uses lowercase — must match case-insensitively
    monkeypatch.setattr(_subprocess, "run", _make_fake_run({"Accuracy": 0.95}))

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "MET" in out
    assert "NOT MET" not in out
    assert "NOT EVALUABLE" not in out
    assert "0.95" in out


def test_compute_run_local_criterion_not_met(tmp_path, monkeypatch, capsys):
    """Criterion NOT MET: emits loss=0.8, criterion loss <= 0.5 => NOT MET."""
    import subprocess as _subprocess

    spec = {
        "command": "echo hi",
        "success_criterion": {"metric": "loss", "operator": "<=", "threshold": 0.5},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    monkeypatch.setattr(_subprocess, "run", _make_fake_run({"loss": 0.8}))

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "NOT MET" in out
    assert "0.8" in out


def test_compute_run_local_criterion_not_evaluable_missing_metric(tmp_path, monkeypatch, capsys):
    """NOT EVALUABLE (fail-closed): criterion metric absent from emitted result."""
    import subprocess as _subprocess

    spec = {
        "command": "echo hi",
        "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    # Result file contains a different metric — 'accuracy' not present
    monkeypatch.setattr(_subprocess, "run", _make_fake_run({"loss": 0.1}))

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "NOT EVALUABLE" in out


def test_compute_run_local_criterion_not_evaluable_no_result_file(tmp_path, monkeypatch, capsys):
    """NOT EVALUABLE (fail-closed): no result file written at all."""
    import subprocess as _subprocess

    spec = {
        "command": "echo hi",
        "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    # fake_run writes nothing to BLUEPRINT_RESULT_FILE
    monkeypatch.setattr(_subprocess, "run", _make_fake_run(None))

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "NOT EVALUABLE" in out


def test_compute_run_local_criterion_via_cli_flags(tmp_path, monkeypatch, capsys):
    """Criterion supplied via --criterion-metric/--criterion-op/--criterion-threshold."""
    import subprocess as _subprocess

    monkeypatch.setattr(_subprocess, "run", _make_fake_run({"f1": 0.88}))

    args = blueprint.build_parser().parse_args([
        "compute", "run", "echo hi",
        "--criterion-metric", "f1",
        "--criterion-op", ">=",
        "--criterion-threshold", "0.8",
    ])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "MET" in out
    assert "0.88" in out


def test_compute_run_local_no_criterion_unchanged(tmp_path, monkeypatch, capsys):
    """Back-compat: spec with only command/declared_metric and no success_criterion emits no verdict."""
    import subprocess as _subprocess

    spec = {"command": "echo hi", "declared_metric": "loss"}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    monkeypatch.setattr(_subprocess, "run", _make_fake_run({"loss": 0.3}))

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "verdict" not in out.lower() or "NOT EVALUABLE" not in out
    assert "MET" not in out
    assert "SUCCEEDED" in out


def test_compute_run_local_criterion_verdict_auc_case_mismatch(tmp_path, monkeypatch, capsys):
    """Case-insensitive capture->eval loop: emits AUC=0.9, criterion auc >= 0.8 => MET."""
    import subprocess as _subprocess

    spec = {
        "command": "echo hi",
        "success_criterion": {"metric": "auc", "operator": ">=", "threshold": 0.8},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    # Command emits uppercase key; criterion uses lowercase — must match case-insensitively
    monkeypatch.setattr(_subprocess, "run", _make_fake_run({"AUC": 0.9}))

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "MET" in out
    assert "NOT MET" not in out
    assert "0.9" in out


def test_compute_run_local_two_arm_metric_isolation(tmp_path, monkeypatch, capsys):
    """Each arm writes to its own isolated result file; both values are captured independently (not last-arm-wins)."""
    import subprocess as _subprocess

    spec = {
        "declared_metric": "score",
        "arms": [
            {"name": "arm_a", "command": "echo arm_a"},
            {"name": "arm_b", "command": "echo arm_b"},
        ],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run_arms(cmd, env=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None
        if rf:
            # Each arm writes a distinct score value to its own result file.
            # arm_a gets 0.7; arm_b gets 0.9 — determined by the path suffix.
            value = 0.7 if "arm_a" in rf else 0.9
            with open(rf, "w") as f:
                json.dump({"score": value}, f)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run_arms)

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    assert "SUCCEEDED" in out
    # Both arm values must appear in output — not overwritten by last arm
    assert "arm_a" in out
    assert "arm_b" in out
    assert "0.7" in out
    assert "0.9" in out
    # Verify the JSON result structure directly by re-running and capturing result
    import io as _io
    captured = _io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    args2 = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args2)
    # The render output shows per-arm values; both arms' distinct metric values present
    out2 = captured.getvalue()
    assert "arm_a" in out2 and "0.7" in out2
    assert "arm_b" in out2 and "0.9" in out2


def test_compute_run_local_arms_one_failure_captured(tmp_path, monkeypatch, capsys):
    """One arm fails; the other arm's metric is still captured (failure does not suppress success)."""
    import subprocess as _subprocess

    spec = {
        "declared_metric": "accuracy",
        "arms": [
            {"name": "good_arm", "command": "echo good"},
            {"name": "bad_arm", "command": "exit 1"},
        ],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run_mixed(cmd, env=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None

        class R:
            pass

        if rf and "good_arm" in rf:
            with open(rf, "w") as f:
                json.dump({"accuracy": 0.85}, f)
            R.returncode = 0
        else:
            R.returncode = 1

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run_mixed)

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint.cmd_compute(args)

    out = capsys.readouterr().out
    # Overall status reflects the failure
    assert "FAILED" in out
    # The good arm's metric must still appear
    assert "good_arm" in out
    assert "0.85" in out
    # The bad arm is marked as failed, not silently dropped
    assert "bad_arm" in out
    assert "FAILED" in out


def test_compute_run_local_arms_emit_canonical_shape(tmp_path, monkeypatch, capsys):
    """Per-arm entries match the canonical aggregator shape on both success and failure."""
    import subprocess as _subprocess

    spec = {
        "declared_metric": "accuracy",
        "arms": [
            {"name": "good_arm", "command": "echo good"},
            {"name": "bad_arm", "command": "exit 1"},
        ],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run_mixed(cmd, env=None, stderr=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None

        class R:
            pass

        if rf and "good_arm" in rf:
            with open(rf, "w") as f:
                json.dump({"accuracy": 0.85}, f)
            R.returncode = 0
        else:
            R.returncode = 1

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run_mixed)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["--json", "compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    arms_block = data["metrics"]["arms"]

    # Success arm: {declared_metric: value, exit_code: 0} — no legacy __failed__ key
    good = arms_block["good_arm"]
    assert good["accuracy"] == 0.85
    assert good["exit_code"] == 0
    assert "__failed__" not in good
    assert "error" not in good

    # Failure arm: {error: reason, exit_code: N} — no legacy __failed__ / __exit_code__ keys
    bad = arms_block["bad_arm"]
    assert "error" in bad
    assert bad["exit_code"] == 1
    assert "__failed__" not in bad
    assert "__exit_code__" not in bad
    # Reason should be a classified string mentioning the exit code
    assert "exit 1" in bad["error"] or "crash" in bad["error"]


def test_compute_run_arms_success_extracts_declared_metric(tmp_path, monkeypatch, capsys):
    """Successful arm with declared_metric emits only {declared_metric, exit_code}, not the full dict."""
    import subprocess as _subprocess

    spec = {
        "declared_metric": "loss",
        "arms": [{"name": "arm_a", "command": "echo hi"}],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run(cmd, env=None, stderr=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None
        if rf:
            with open(rf, "w") as f:
                json.dump({"loss": 0.12, "extra_field": "noise"}, f)

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["--json", "compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    arm = data["metrics"]["arms"]["arm_a"]
    assert arm["loss"] == 0.12
    assert arm["exit_code"] == 0
    # The canonical shape extracts only the declared metric (plus exit_code)
    assert "extra_field" not in arm


def test_compute_run_arms_exit0_metric_missing_emits_unmeasured_reason(tmp_path, monkeypatch, capsys):
    """Arm exits 0 but declared_metric is absent from result → unmeasured criterion error."""
    import subprocess as _subprocess

    spec = {
        "declared_metric": "auc",
        "arms": [{"name": "arm_x", "command": "echo hi"}],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run(cmd, env=None, stderr=None, **kwargs):
        rf = env.get("BLUEPRINT_RESULT_FILE") if env else None
        if rf:
            # Writes result file but WITHOUT the declared metric
            with open(rf, "w") as f:
                json.dump({"wrong_metric": 0.5}, f)

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["--json", "compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    arm = data["metrics"]["arms"]["arm_x"]
    assert "error" in arm
    assert arm["exit_code"] == 0
    assert "unmeasured criterion" in arm["error"]
    assert "auc" in arm["error"]


def test_compute_run_arms_stderr_captured_in_failure_entry(tmp_path, monkeypatch, capsys):
    """Failed arm's stderr output appears as stderr_excerpt in the canonical entry."""
    import subprocess as _subprocess

    spec = {
        "declared_metric": "score",
        "arms": [{"name": "failing_arm", "command": "echo oops >&2; exit 2"}],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run(cmd, env=None, stderr=None, **kwargs):
        # Write stderr content to the captured file handle if provided
        if stderr is not None and hasattr(stderr, "write"):
            stderr.write(b"Traceback: something went wrong\n")

        class R:
            returncode = 2
        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["--json", "compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    arm = data["metrics"]["arms"]["failing_arm"]
    assert "error" in arm
    assert arm["exit_code"] == 2
    assert "stderr_excerpt" in arm
    assert "Traceback" in arm["stderr_excerpt"]


# ── GPU detection tests ───────────────────────────────────────────────────────


def test_detect_local_gpu_no_gpu(monkeypatch):
    """_detect_local_gpu returns detected=False when neither nvidia-smi nor rocm-smi is present."""
    monkeypatch.setattr(blueprint.shutil, "which", lambda cmd: None)
    result = blueprint._detect_local_gpu()
    assert result["detected"] is False
    assert result["backend"] is None
    assert result["devices"] == []


def test_detect_local_gpu_nvidia_found(monkeypatch):
    """_detect_local_gpu returns detected=True with backend=nvidia and device name from nvidia-smi."""
    import subprocess as _subprocess

    def fake_which(cmd):
        return "/usr/bin/nvidia-smi" if cmd == "nvidia-smi" else None

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = "Tesla T4\n"
        return R()

    monkeypatch.setattr(blueprint.shutil, "which", fake_which)
    monkeypatch.setattr(_subprocess, "run", fake_run)

    result = blueprint._detect_local_gpu()
    assert result["detected"] is True
    assert result["backend"] == "nvidia"
    assert "Tesla T4" in result["devices"]


def test_detect_local_gpu_nvidia_fails_gracefully(monkeypatch):
    """_detect_local_gpu returns detected=False when nvidia-smi is present but returns non-zero."""
    import subprocess as _subprocess

    def fake_which(cmd):
        return "/usr/bin/nvidia-smi" if cmd == "nvidia-smi" else None

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
        return R()

    monkeypatch.setattr(blueprint.shutil, "which", fake_which)
    monkeypatch.setattr(_subprocess, "run", fake_run)

    result = blueprint._detect_local_gpu()
    assert result["detected"] is False


def test_detect_local_gpu_nvidia_exception_soft_fail(monkeypatch):
    """_detect_local_gpu returns detected=False when subprocess.run raises (e.g. timeout)."""
    import subprocess as _subprocess

    def fake_which(cmd):
        return "/usr/bin/nvidia-smi" if cmd == "nvidia-smi" else None

    def fake_run(cmd, **kwargs):
        raise _subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(blueprint.shutil, "which", fake_which)
    monkeypatch.setattr(_subprocess, "run", fake_run)

    result = blueprint._detect_local_gpu()
    assert result["detected"] is False


def test_compute_run_detects_and_reports_local_gpu(tmp_path, monkeypatch, capsys):
    """compute run reports the detected GPU in output and injects BLUEPRINT_LOCAL_GPU into the subprocess env."""
    import subprocess as _subprocess

    injected_env = {}

    def fake_run(cmd, env=None, **kwargs):
        if env:
            injected_env.update(env)
            rf = env.get("BLUEPRINT_RESULT_FILE")
            if rf:
                import json as _json
                with open(rf, "w") as f:
                    _json.dump({"accuracy": 0.9}, f)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": True, "backend": "nvidia", "devices": ["Tesla T4"],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "echo hi"])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    assert "gpu" in out
    assert "Tesla T4" in out
    assert injected_env.get("BLUEPRINT_LOCAL_GPU") == "Tesla T4"


def test_compute_run_no_gpu_reports_none(tmp_path, monkeypatch, capsys):
    """compute run reports 'gpu : none' and injects BLUEPRINT_LOCAL_GPU=none when no GPU detected."""
    import subprocess as _subprocess

    injected_env = {}

    def fake_run(cmd, env=None, **kwargs):
        if env:
            injected_env.update(env)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "echo hi"])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    assert "gpu         : none" in out
    assert injected_env.get("BLUEPRINT_LOCAL_GPU") == "none"


def test_compute_run_gpu_in_json_result(tmp_path, monkeypatch, capsys):
    """compute run --json includes 'gpu' key with detected field in the emitted JSON."""
    import subprocess as _subprocess

    def fake_run(cmd, env=None, **kwargs):
        if env:
            rf = env.get("BLUEPRINT_RESULT_FILE")
            if rf:
                import json as _json
                with open(rf, "w") as f:
                    _json.dump({"loss": 0.3}, f)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": True, "backend": "nvidia", "devices": ["RTX 4090"],
    })

    args = blueprint.build_parser().parse_args(["--json", "compute", "run", "echo hi"])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert "gpu" in data
    assert data["gpu"]["detected"] is True
    assert data["gpu"]["backend"] == "nvidia"
    assert "RTX 4090" in data["gpu"]["devices"]


def test_compute_run_arms_gpu_detected(tmp_path, monkeypatch, capsys):
    """compute run with arms also reports the GPU line when a GPU is detected."""
    import subprocess as _subprocess

    spec = {
        "arms": [
            {"name": "arm_a", "command": "echo hi"},
        ],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run(cmd, env=None, **kwargs):
        if env:
            rf = env.get("BLUEPRINT_RESULT_FILE")
            if rf:
                import json as _json
                with open(rf, "w") as f:
                    _json.dump({"score": 1.0}, f)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": True, "backend": "nvidia", "devices": ["Tesla V100"],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    assert "gpu" in out
    assert "Tesla V100" in out


def test_compute_run_arms_json_mode_produces_valid_json(tmp_path, monkeypatch, capsys):
    """compute run --json with a multi-arm spec emits valid JSON with no extra text."""
    import subprocess as _subprocess

    spec = {
        "arms": [
            {"name": "arm_a", "command": "echo hi"},
            {"name": "arm_b", "command": "echo bye"},
        ],
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    def fake_run(cmd, env=None, **kwargs):
        if env:
            rf = env.get("BLUEPRINT_RESULT_FILE")
            if rf:
                import json as _json
                with open(rf, "w") as f:
                    _json.dump({"score": 0.9}, f)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["--json", "compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    out = capsys.readouterr().out
    # Must be parseable as JSON with no leading text from per-arm prints
    data = json.loads(out)
    assert "status" in data
    assert "metrics" in data
    arms_block = data["metrics"]["arms"]
    assert "arm_a" in arms_block
    assert "arm_b" in arms_block


# ---------------------------------------------------------------------------
# Container-image wrapping tests for _compute_run_local (single-command + arms)
# ---------------------------------------------------------------------------


def test_compute_run_local_single_command_wrapped_in_docker(tmp_path, monkeypatch, capsys):
    """When container_image is set, the single-command path runs 'docker run' not bare bash."""
    import subprocess as _subprocess

    spec = {
        "command": "python train.py",
        "container_image": "myregistry.io/myimage:latest",
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    captured_cmds = []

    def fake_run(cmd, env=None, **kwargs):
        captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    # The run command must use docker, not bare bash
    run_cmds = [c for c in captured_cmds if isinstance(c, list) and c[:2] == ["docker", "run"]]
    assert run_cmds, f"expected a 'docker run' invocation; got commands: {captured_cmds}"
    docker_cmd = run_cmds[0]
    assert "myregistry.io/myimage:latest" in docker_cmd
    assert "BLUEPRINT_RESULT_FILE" in " ".join(docker_cmd)
    # No --gpus when no GPU detected
    assert "--gpus" not in docker_cmd


def test_compute_run_local_single_command_docker_gpus_when_gpu_detected(tmp_path, monkeypatch, capsys):
    """When container_image is set and a GPU is detected, --gpus all is added to docker run."""
    import subprocess as _subprocess

    spec = {
        "command": "python train.py",
        "container_image": "myregistry.io/myimage:cuda",
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    captured_cmds = []

    def fake_run(cmd, env=None, **kwargs):
        captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": True, "backend": "nvidia", "devices": ["Tesla T4"],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    run_cmds = [c for c in captured_cmds if isinstance(c, list) and c[:2] == ["docker", "run"]]
    assert run_cmds, f"expected 'docker run'; got: {captured_cmds}"
    docker_cmd = run_cmds[0]
    assert "--gpus" in docker_cmd
    assert "all" in docker_cmd


def test_compute_run_local_no_container_uses_bare_bash(tmp_path, monkeypatch, capsys):
    """When container_image is absent, the command runs via bare bash -lc (unchanged path)."""
    import subprocess as _subprocess

    captured_cmds = []

    def fake_run(cmd, env=None, **kwargs):
        captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
        if env:
            rf = env.get("BLUEPRINT_RESULT_FILE")
            if rf:
                with open(rf, "w") as f:
                    json.dump({"loss": 0.5}, f)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "echo hi"])
    blueprint._compute_run_local(args)

    assert any(
        isinstance(c, list) and c[:2] == ["bash", "-lc"]
        for c in captured_cmds
    ), f"expected bare 'bash -lc'; got: {captured_cmds}"
    # No docker invocation
    assert not any(
        isinstance(c, list) and "docker" in c
        for c in captured_cmds
    ), f"expected no docker call; got: {captured_cmds}"


def test_compute_run_local_arms_wrapped_in_docker_per_arm(tmp_path, monkeypatch, capsys):
    """When container_image is set with arms, each arm command runs via a separate docker run."""
    import subprocess as _subprocess

    spec = {
        "arms": [
            {"name": "adam", "command": "python train.py --opt adam"},
            {"name": "sgd", "command": "python train.py --opt sgd"},
        ],
        "declared_metric": "val_loss",
        "container_image": "myregistry.io/myimage:latest",
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    captured_cmds = []

    def fake_run(cmd, env=None, **kwargs):
        captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    docker_run_cmds = [
        c for c in captured_cmds if isinstance(c, list) and c[:2] == ["docker", "run"]
    ]
    # One docker run per arm
    assert len(docker_run_cmds) == 2, f"expected 2 docker run invocations; got: {captured_cmds}"
    for docker_cmd in docker_run_cmds:
        assert "myregistry.io/myimage:latest" in docker_cmd
        assert "BLUEPRINT_RESULT_FILE" in " ".join(docker_cmd)
        assert "--gpus" not in docker_cmd  # no GPU detected


# ---------------------------------------------------------------------------
# docker-login alignment tests (_compute_docker_login / _compute_run_local)
# ---------------------------------------------------------------------------


def test_compute_run_docker_login_password_only_and_failure(tmp_path, monkeypatch):
    """Login is attempted when only a password is supplied; a failed login aborts before pull."""
    import subprocess as _subprocess

    spec = {
        "command": "python train.py",
        "container_image": "nvcr.io/nvidia/pytorch:24.01",
        "container_registry_auth": {"password": "secret"},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    login_calls = []

    class _FailResult:
        returncode = 1
        stderr = b"unauthorized"

    class _OkResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kwargs):
        login_calls.append(list(cmd) if isinstance(cmd, list) else cmd)
        if cmd[0] == "docker" and cmd[1] == "login":
            return _FailResult()
        return _OkResult()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    with pytest.raises(SystemExit) as exc_info:
        blueprint._compute_run_local(args)

    assert exc_info.value.code == 1
    # login was attempted
    login_invocations = [c for c in login_calls if isinstance(c, list) and c[:2] == ["docker", "login"]]
    assert login_invocations, "docker login must be called when password is present"
    # docker pull was NOT called after the failed login
    pull_invocations = [c for c in login_calls if isinstance(c, list) and "pull" in c]
    assert not pull_invocations, "docker pull must not be called after a failed login"


def test_compute_run_docker_login_password_only_gates_login(tmp_path, monkeypatch):
    """docker login is attempted even when server and username are absent, if password is set."""
    import subprocess as _subprocess

    spec = {
        "command": "echo hi",
        "container_image": "nvcr.io/nvidia/pytorch:24.01",
        "container_registry_auth": {"password": "tok3n"},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    login_cmds = []

    class _R:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "login"]:
            login_cmds.append(list(cmd))
        return _R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    assert login_cmds, "expected docker login to be called when password is present without server/username"
    login_cmd = login_cmds[0]
    assert "--password-stdin" in login_cmd
    # no --username flag because username was not set
    assert "--username" not in login_cmd
    # no server appended because server was not set
    assert "nvcr.io" not in login_cmd


def test_compute_run_docker_login_skipped_when_no_password(tmp_path, monkeypatch):
    """docker login is not called when container_registry_auth lacks a password."""
    import subprocess as _subprocess

    spec = {
        "command": "echo hi",
        "container_image": "myregistry.io/img:latest",
        "container_registry_auth": {"server": "myregistry.io", "username": "user"},
    }
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))

    login_calls = []

    class _R:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "login"]:
            login_calls.append(list(cmd))
        return _R()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(blueprint, "_detect_local_gpu", lambda: {
        "detected": False, "backend": None, "devices": [],
    })

    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(spec_file)])
    blueprint._compute_run_local(args)

    assert not login_calls, "docker login must not be called when password is absent"


# ── tasks create ─────────────────────────────────────────────────────────────

def test_tasks_create_posts_task(mock_api, monkeypatch, capsys):
    """tasks create POSTs to /api/admin/tasks with correct fields."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    rc = blueprint.main(["tasks", "create", "My new task", "Do the thing.", "--yes"])
    assert rc == 0
    create_calls = [(m, p, b) for m, p, b in mock_api if m == "POST" and "/api/admin/tasks" in p and "compute-experiment" not in p]
    assert create_calls, "Expected a POST to /api/admin/tasks"
    body = create_calls[0][2]
    assert body["title"] == "My new task"
    assert body["prompt"] == "Do the thing."
    assert body["github_repo"] == "owner/repo"
    assert body["task_type"] == "create"
    assert body["base_branch"] == "main"


def test_tasks_create_custom_type_and_repo(mock_api, monkeypatch, capsys):
    """tasks create respects --type, --repo, and --base-branch overrides."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "default/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "default/repo", "default_base_branch": "main",
    })
    rc = blueprint.main([
        "tasks", "create", "Fix bug", "Patch the thing.",
        "--type", "fix", "--repo", "myorg/myrepo", "--base-branch", "dev", "--yes",
    ])
    assert rc == 0
    create_calls = [(m, p, b) for m, p, b in mock_api if m == "POST" and "/api/admin/tasks" in p and "compute-experiment" not in p]
    assert create_calls
    body = create_calls[0][2]
    assert body["task_type"] == "fix"
    assert body["github_repo"] == "myorg/myrepo"
    assert body["base_branch"] == "dev"


def test_tasks_create_read_only_refused(mock_api, monkeypatch):
    """tasks create refuses under a read-only (agent-JWT) config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "create", "T", "P", "--yes"])
    assert exc.value.code == 1


def test_tasks_create_aborts_without_yes(mock_api, monkeypatch, capsys):
    """tasks create aborts on non-interactive stdin without --yes."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["tasks", "create", "T", "P"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aborted" in out
    assert not any(m == "POST" and "/api/admin/tasks" in p and "compute-experiment" not in p for m, p, _ in mock_api)


def test_tasks_create_missing_repo_errors(mock_api, monkeypatch, capsys):
    """tasks create exits with error when no repo is configured or supplied."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y", "default_repo": "",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y", "default_repo": "",
    })
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["tasks", "create", "T", "P", "--yes"])
    assert exc.value.code == 1


# ── compute launch ────────────────────────────────────────────────────────────

def test_compute_launch_creates_cloud_experiment(mock_api, monkeypatch, capsys):
    """compute launch --dud --yes POSTs to /api/admin/tasks/compute-experiment with correct body."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    rc = blueprint.main(["compute", "launch", "--dud", "--yes"])
    assert rc == 0
    launch_calls = [(m, p, b) for m, p, b in mock_api if m == "POST" and "compute-experiment" in p]
    assert launch_calls, "Expected a POST to /api/admin/tasks/compute-experiment"
    body = launch_calls[0][2]
    assert body["github_repo"] == "owner/repo"
    assert "dud_ok" in body["command"]
    assert body["success_criterion"]["metric"] == "dud_ok"
    assert body["success_criterion"]["operator"] == ">="
    assert body["success_criterion"]["threshold"] == 1.0
    assert body["spend_cap_usd"] == 0.50
    assert body["deadline_seconds"] == 600


def test_compute_launch_custom_command(mock_api, monkeypatch, capsys):
    """compute launch with explicit --command and --metric posts the correct criterion."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    rc = blueprint.main([
        "compute", "launch",
        "--repo", "myorg/ml",
        "--command", "python3 train.py",
        "--metric", "accuracy",
        "--operator", ">=",
        "--threshold", "0.9",
        "--title", "Train run",
        "--yes",
    ])
    assert rc == 0
    launch_calls = [(m, p, b) for m, p, b in mock_api if m == "POST" and "compute-experiment" in p]
    assert launch_calls
    body = launch_calls[0][2]
    assert body["github_repo"] == "myorg/ml"
    assert body["command"] == "python3 train.py"
    assert body["success_criterion"]["metric"] == "accuracy"
    assert body["success_criterion"]["threshold"] == 0.9
    assert body["title"] == "Train run"


def test_compute_launch_read_only_refused(mock_api, monkeypatch):
    """compute launch refuses under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["compute", "launch", "--dud", "--yes"])
    assert exc.value.code == 1


def test_compute_launch_aborts_without_yes(mock_api, monkeypatch, capsys):
    """compute launch aborts on non-interactive stdin without --yes."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y",
        "default_repo": "owner/repo", "default_base_branch": "main",
    })
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["compute", "launch", "--dud"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aborted" in out
    assert not any(m == "POST" and "compute-experiment" in p for m, p, _ in mock_api)


def test_compute_launch_no_command_errors(mock_api, monkeypatch):
    """compute launch without --dud or --command exits with error."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {
        "api_url": "x", "api_key": "y", "default_repo": "owner/repo",
    })
    monkeypatch.setattr(blueprint, "load_config", lambda: {
        "api_url": "x", "api_key": "y", "default_repo": "owner/repo",
    })
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["compute", "launch", "--yes"])
    assert exc.value.code == 1


# ── messages answer and confirm ───────────────────────────────────────────────

def test_messages_answer_targets_real_endpoint(mock_api, monkeypatch, capsys):
    """messages answer calls PATCH /api/admin/messages/{id} with resolution body,
    not a fabricated POST to /projects/{pid}/messages/{id}/answer."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    msg_id = "msg-uuid-0001"
    rc = blueprint.main(["messages", "answer", msg_id, "This is my answer"])
    assert rc == 0
    # Must have called PATCH /api/admin/messages/{msg_id}
    patch_calls = [(m, p, b) for m, p, b in mock_api
                   if m == "PATCH" and f"/messages/{msg_id}" in p]
    assert patch_calls, "Expected a PATCH to /api/admin/messages/{msg_id}, none recorded"
    _, path, body = patch_calls[0]
    assert "resolution" in body, "Body must contain 'resolution' field"
    assert body["resolution"] == "This is my answer"
    # The old /answer POST route must NOT have been called
    old_route_calls = [p for _, p, _ in mock_api if "/answer" in p]
    assert not old_route_calls, f"Old /answer route was called: {old_route_calls}"


def test_messages_confirm_list_json(mock_api, capsys):
    """messages confirm list --json returns pending_user_confirmation from project."""
    rc = blueprint.main(["--json", "messages", "confirm", "list"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "pending" in parsed
    assert len(parsed["pending"]) == 2
    ids = [p["id"] for p in parsed["pending"]]
    assert "pend-abc123" in ids
    assert "pend-def456" in ids
    # Must have fetched the project
    assert any("/projects/proj-1" in p for _, p, _ in mock_api)


def test_messages_confirm_list_human_render(mock_api, capsys):
    """messages confirm list (human) shows pending IDs, types, and proposals."""
    rc = blueprint.main(["messages", "confirm", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PENDING CONFIRMATIONS" in out
    assert "pend-abc123" in out
    assert "major_decision" in out
    assert "Should we adopt" in out


def test_messages_confirm_accept_fires_with_yes(mock_api, monkeypatch, capsys):
    """messages confirm accept --yes POSTs to /pending-confirm/{id}/accept."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["messages", "confirm", "accept", "pend-abc123",
                         "--answer", "Yes, proceed", "--yes"])
    assert rc == 0
    accept_calls = [(m, p, b) for m, p, b in mock_api
                    if m == "POST" and "/pending-confirm/pend-abc123/accept" in p]
    assert accept_calls, "Expected POST to /pending-confirm/pend-abc123/accept"
    _, _, body = accept_calls[0]
    assert body.get("answer") == "Yes, proceed"
    out = capsys.readouterr().out
    assert "Accepted" in out


def test_messages_confirm_accept_aborts_without_yes(mock_api, monkeypatch, capsys):
    """messages confirm accept aborts on non-interactive stdin without --yes."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = blueprint.main(["messages", "confirm", "accept", "pend-abc123"])
    assert rc == 0
    accept_calls = [p for m, p, _ in mock_api
                    if m == "POST" and "/pending-confirm/" in p]
    assert not accept_calls, "accept must not fire without --yes on non-interactive stdin"
    out = capsys.readouterr().out
    assert "Aborted" in out


def test_messages_confirm_dismiss_fires_with_yes(mock_api, monkeypatch, capsys):
    """messages confirm dismiss --yes POSTs to /pending-confirm/{id}/dismiss."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"api_url": "x", "api_key": "y"})
    rc = blueprint.main(["messages", "confirm", "dismiss", "pend-def456",
                         "--note", "Not needed anymore", "--yes"])
    assert rc == 0
    dismiss_calls = [(m, p, b) for m, p, b in mock_api
                     if m == "POST" and "/pending-confirm/pend-def456/dismiss" in p]
    assert dismiss_calls, "Expected POST to /pending-confirm/pend-def456/dismiss"
    _, _, body = dismiss_calls[0]
    assert body.get("note") == "Not needed anymore"
    out = capsys.readouterr().out
    assert "Dismissed" in out


def test_messages_confirm_read_only_refused(mock_api, monkeypatch):
    """messages confirm accept/dismiss refuse under a read-only config."""
    monkeypatch.setattr(blueprint_api, "load_config", lambda: {"read_only": True})
    monkeypatch.setattr(blueprint, "load_config", lambda: {"read_only": True})
    with pytest.raises(SystemExit) as exc:
        blueprint.main(["messages", "confirm", "accept", "pend-abc123", "--yes"])
    assert exc.value.code == 1
