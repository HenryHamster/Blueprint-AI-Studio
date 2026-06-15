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
    ["tasks"],
    ["tasks", "list"],
    ["tasks", "show"],
    ["tasks", "cancel"],
    ["tasks", "requeue"],
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
                },
                "recent_versions": [
                    {"version_number": 11, "phase": "stabilize",
                     "status": "promoted", "promoted_sha": "deadbeef",
                     "merged_task_count": 10},
                ],
                "promotion_enabled_effective": True,
                "promotion_enabled_source": "fleet",
            }
        if "/tasks/" in path:
            return {"id": "task-123", "title": "demo", "task_type": "create",
                    "status": "IN_PROGRESS", "created_at": "2026-06-15T00:00:00Z"}
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
