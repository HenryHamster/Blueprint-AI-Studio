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
        if "deploy-state" in path:
            return {"status": "RUNNING", "current_revision": "rev-1",
                    "max_concurrent_vms": 5}
        if "settings" in path:
            return {"max_concurrent_vms": 5, "promotion_enabled": True}
        if "version-lifecycle" in path:
            return {
                "current_version": {
                    "version_number": 12, "phase": "build",
                    "qualification_state": "qualifying", "merged_task_count": 3,
                    "active_candidate_sha": "abc123",
                    "last_qualification": {"status": "pass"},
                },
                "recent_versions": [
                    {"version_number": 11, "phase": "stabilize",
                     "status": "promoted", "promoted_sha": "deadbeef",
                     "merged_task_count": 10},
                ],
                "promotion_readiness": {"ready": False, "reason": "audit pending"},
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
    assert parsed["promotion"]["enabled_effective"] is True


def test_status_json_flag_after_subcommand(mock_api, capsys):
    """--json is accepted both before and after the subcommand."""
    rc = blueprint.main(["status", "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["deploy_state"]["status"] == "RUNNING"


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
