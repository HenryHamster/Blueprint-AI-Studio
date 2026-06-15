"""Smoke tests for the blueprint operator CLI.

Verifies the argparse tree parses (every subcommand exposes --help), that the
module imports cleanly, and that --json emits valid JSON for a read command —
all with blueprint_api.api mocked so no network/credentials are needed.
"""
import json

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
                        {"title": "demo task", "pr_number": 42,
                         "task_type": "create"},
                    ],
                },
                "recent_versions": [
                    {"id": "ver-11-id", "version_number": 11, "phase": "stabilize",
                     "status": "promoted", "promoted_sha": "deadbeef",
                     "merged_task_count": 10},
                ],
                "promotion_enabled_effective": True,
                "promotion_enabled_source": "fleet",
            }
        if "/release-versions/" in path and "/summary" in path:
            return {
                "tasks_merged": [
                    {"title": "historical task", "pr_number": 7,
                     "task_type": "fix"},
                ],
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
    # promotion fields have no equivalent in /api/admin/state — emitted as None
    assert parsed["promotion"]["enabled_effective"] is None
    # single /api/admin/state call — no deploy-state or settings calls
    assert any("/admin/state" in path for _, path, _ in mock_api)
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
    assert len(parsed["tasks_merged"]) == 1
    assert parsed["tasks_merged"][0]["pr_number"] == 42


def test_versions_show_changelog_human_render(mock_api, capsys):
    """Human-readable versions show includes task titles and PR numbers."""
    rc = blueprint.main(["versions", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#42" in out
    assert "demo task" in out


def test_versions_show_historical_fetches_summary(mock_api, capsys):
    """versions show <N> for a historical version fetches the per-version summary."""
    rc = blueprint.main(["--json", "versions", "show", "11"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["version_number"] == 11
    assert isinstance(parsed["tasks_merged"], list)
    # historical fixture returns one task via the /summary endpoint
    assert parsed["tasks_merged"][0]["pr_number"] == 7
    # the /summary endpoint must have been called
    assert any("/release-versions/" in p and "/summary" in p
               for _, p, _ in mock_api)


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
