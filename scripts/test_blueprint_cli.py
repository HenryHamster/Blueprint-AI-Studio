"""Smoke tests for the blueprint operator CLI.

Verifies the argparse tree parses (every subcommand exposes --help), that the
module imports cleanly, and that --json emits valid JSON for a read command —
all with blueprint_api.api mocked so no network/credentials are needed.
"""
import io
import json
import urllib.error
import urllib.request

import pytest

import blueprint
import blueprint_api


# ── --help for every (sub)command parses and exits 0 ─────────────────────────

HELP_INVOCATIONS = [
    [],
    ["status"],
    ["diagnostics"],
    ["tasks"],
    ["tasks", "list"],
    ["tasks", "show"],
    ["tasks", "cancel"],
    ["tasks", "requeue"],
    ["tasks", "scope"],
    ["tasks", "tree"],
    ["tasks", "failures"],
    ["versions"],
    ["versions", "list"],
    ["versions", "show"],
    ["versions", "export-changelog"],
    ["promote-recover"],
    ["logs"],
    ["logs", "errors"],
    ["logs", "tail"],
    ["logs", "search"],
    ["logs", "task"],
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
        if "/tasks" in path:
            return {"tasks": [{"id": "task-123", "task_type": "create",
                               "status": "IN_PROGRESS", "title": "demo"}],
                    "total": 1}
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
    assert "#" not in md
