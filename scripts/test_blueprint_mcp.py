"""Contract tests for the read-only Blueprint MCP adapter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import blueprint_mcp


PROJECT_ID = "11111111-1111-4111-8111-111111111111"
TASK_ID = "22222222-2222-4222-8222-222222222222"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _modern_meta(version: str = blueprint_mcp.PROTOCOL_VERSION) -> dict:
    return {
        "_meta": {
            blueprint_mcp.PROTOCOL_VERSION_META_KEY: version,
            blueprint_mcp.CLIENT_INFO_META_KEY: {
                "name": "blueprint-mcp-tests",
                "version": "1.0.0",
            },
            blueprint_mcp.CLIENT_CAPABILITIES_META_KEY: {},
        }
    }


def _legacy_initialize_params(version: str) -> dict:
    return {
        "protocolVersion": version,
        "capabilities": {},
        "clientInfo": {"name": "blueprint-mcp-tests", "version": "1.0.0"},
    }


def _profile() -> dict:
    return {
        "schema_version": "blueprint.agent-access.v1",
        "credential": {
            "id": "33333333-3333-4333-8333-333333333333",
            "name": "test-coding-agent",
            "principal_user_id": "service:credential:test",
            "scopes": ["coding-agent:read"],
            "expires_at": None,
        },
        "projects": [
            {
                "id": PROJECT_ID,
                "name": "Blueprint",
                "github_owner": "HenryHamster",
                "github_repo": "BlueprintAIStudio",
                "role": "viewer",
            }
        ],
        "capabilities": ["project.snapshot", "tasks.list", "tasks.read"],
        "server": {"revision": "test"},
    }


def _tool_payload(response: dict) -> tuple[dict, bool]:
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    assert result["structuredContent"] == payload
    return payload, result["isError"]


def _configure_scoped_agent(monkeypatch) -> None:
    monkeypatch.setenv("BLUEPRINT_API_URL", "https://blueprint.example")
    monkeypatch.setenv("BLUEPRINT_API_KEY", "bpsc_test-token")
    monkeypatch.delenv("BLUEPRINT_CONFIG", raising=False)
    monkeypatch.setattr(
        blueprint_mcp,
        "load_config",
        lambda: {
            "api_url": "https://blueprint.example",
            "api_key": "bpsc_test-token",
            "default_repo": "HenryHamster/BlueprintAIStudio",
        },
    )


def test_tool_registry_is_discoverable_and_read_only():
    response = blueprint_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {
        "list_projects",
        "get_project_snapshot",
        "list_tasks",
        "get_task",
        "list_messages",
    }
    assert not any(
        fragment in name
        for name in names
        for fragment in ("create", "update", "delete", "cancel", "requeue", "merge")
    )
    assert all(
        tool["annotations"]["readOnlyHint"] is True
        for tool in response["result"]["tools"]
    )
    assert all(
        tool["annotations"]["destructiveHint"] is False
        for tool in response["result"]["tools"]
    )
    assert "resultType" not in response["result"]
    assert "ttlMs" not in response["result"]


def test_server_discover_advertises_modern_protocol_and_cache_metadata():
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "discover-1",
            "method": "server/discover",
            "params": _modern_meta(),
        }
    )

    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == blueprint_mcp.PROTOCOL_VERSION
    assert blueprint_mcp.LEGACY_PROTOCOL_VERSION in result["supportedVersions"]
    assert result["capabilities"] == {"tools": {}}
    assert result["ttlMs"] == blueprint_mcp.PUBLIC_CATALOG_TTL_MS
    assert result["cacheScope"] == "public"
    assert result["_meta"][blueprint_mcp.SERVER_INFO_META_KEY] == {
        "name": "blueprint",
        "version": blueprint_mcp.SERVER_VERSION,
    }
    assert "serverInfo" not in result


def test_modern_tools_list_has_complete_result_and_public_cache_hints():
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/list",
            "params": _modern_meta(),
        }
    )

    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == blueprint_mcp.PUBLIC_CATALOG_TTL_MS
    assert result["cacheScope"] == "public"
    assert result["_meta"][blueprint_mcp.SERVER_INFO_META_KEY]["name"] == "blueprint"


def test_claude_project_registration_is_secret_free_and_portable():
    config = json.loads((REPO_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["blueprint"]
    assert server["type"] == "stdio"
    assert server["command"] == "${BLUEPRINT_PYTHON:-python}"
    assert server["args"] == [
        "${CLAUDE_PROJECT_DIR:-.}/scripts/blueprint_mcp.py"
    ]
    assert server["env"] == {
        "BLUEPRINT_CONFIG": (
            "${BLUEPRINT_AGENT_CONFIG:-__BLUEPRINT_AGENT_CONFIG_NOT_SET__}"
        )
    }
    serialized = json.dumps(config)
    assert "BLUEPRINT_API_KEY" not in serialized
    assert "bpsc_" not in serialized


def test_initialize_does_not_require_or_reveal_credentials(monkeypatch):
    monkeypatch.delenv("BLUEPRINT_CONFIG", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_URL", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_KEY", raising=False)
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": _legacy_initialize_params("2024-11-05"),
        }
    )
    assert response["result"]["serverInfo"]["name"] == "blueprint"
    assert response["result"]["capabilities"] == {"tools": {}}
    assert "Start with get_project_snapshot" in response["result"]["instructions"]

    future = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "initialize",
            "params": _legacy_initialize_params("2099-01-01"),
        }
    )
    assert (
        future["result"]["protocolVersion"]
        == blueprint_mcp.LEGACY_PROTOCOL_VERSION
    )
    assert "resultType" not in future["result"]


def test_json_rpc_parse_invalid_request_and_method_errors():
    parse_error = blueprint_mcp.handle_message(b'{"jsonrpc":"2.0",')
    assert parse_error == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }

    invalid_request = blueprint_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 30, "result": {}}
    )
    assert invalid_request["id"] == 30
    assert invalid_request["error"]["code"] == -32600

    null_id = blueprint_mcp.handle_request(
        {"jsonrpc": "2.0", "id": None, "method": "ping"}
    )
    assert null_id["id"] is None
    assert null_id["result"] == {}

    unknown_method = blueprint_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 31, "method": "blueprint/unknown"}
    )
    assert unknown_method["error"]["code"] == -32601


def test_notifications_never_receive_responses_or_execute_tools(monkeypatch):
    def should_not_run(*_args, **_kwargs):
        raise AssertionError("a tools/call notification must not execute")

    monkeypatch.setattr(blueprint_mcp, "call_tool", should_not_run)
    assert (
        blueprint_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    **_modern_meta(),
                    "name": "list_projects",
                    "arguments": {},
                },
            }
        )
        is None
    )
    assert (
        blueprint_mcp.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        is None
    )


def test_malformed_tool_call_and_unknown_tool_are_protocol_errors():
    malformed = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": ["not", "an", "object"],
        }
    )
    assert malformed["error"]["code"] == -32602

    invalid_arguments = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {"name": "list_projects", "arguments": []},
        }
    )
    assert invalid_arguments["error"]["code"] == -32602

    unknown = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 34,
            "method": "tools/call",
            "params": {"name": "delete_everything", "arguments": {}},
        }
    )
    assert unknown["error"]["code"] == -32602
    assert "Unknown tool" in unknown["error"]["message"]
    assert "result" not in unknown

    schema_violation = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 341,
            "method": "tools/call",
            "params": {
                "name": "list_projects",
                "arguments": {"unexpected": True},
            },
        }
    )
    assert schema_violation["error"]["code"] == -32602
    assert "Invalid arguments" in schema_violation["error"]["message"]


def test_tool_execution_failure_is_visible_but_internal_failure_is_protocol_error(
    monkeypatch,
):
    def api_failure(*_args, **_kwargs):
        raise blueprint_mcp.APIError("Blueprint API is temporarily unavailable")

    monkeypatch.setattr(blueprint_mcp, "call_tool", api_failure)
    execution_error = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 342,
            "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}},
        }
    )
    payload, is_error = _tool_payload(execution_error)
    assert is_error is True
    assert "temporarily unavailable" in payload["error"]

    def internal_failure(*_args, **_kwargs):
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr(blueprint_mcp, "call_tool", internal_failure)
    internal_error = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 343,
            "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}},
        }
    )
    assert internal_error["error"] == {"code": -32603, "message": "Internal error"}


def test_modern_request_metadata_is_validated():
    missing_envelope = blueprint_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 35, "method": "server/discover"}
    )
    assert missing_envelope["error"]["code"] == -32602

    unsupported = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 36,
            "method": "server/discover",
            "params": _modern_meta("2099-01-01"),
        }
    )
    assert unsupported["error"]["code"] == -32022
    assert unsupported["error"]["data"]["requested"] == "2099-01-01"
    assert blueprint_mcp.PROTOCOL_VERSION in unsupported["error"]["data"]["supported"]

    missing_client_info_params = _modern_meta()
    del missing_client_info_params["_meta"][blueprint_mcp.CLIENT_INFO_META_KEY]
    missing_client_info = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 361,
            "method": "tools/list",
            "params": missing_client_info_params,
        }
    )
    assert missing_client_info["result"]["resultType"] == "complete"
    assert missing_client_info["result"]["tools"]

    malformed_client_info_params = _modern_meta()
    malformed_client_info_params["_meta"][blueprint_mcp.CLIENT_INFO_META_KEY] = {
        "name": "client-without-version"
    }
    malformed_client_info = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 362,
            "method": "tools/list",
            "params": malformed_client_info_params,
        }
    )
    assert malformed_client_info["error"]["code"] == -32602


def test_legacy_initialize_requires_capabilities_and_client_info():
    for params, missing_field in (
        ({"protocolVersion": "2025-11-25", "clientInfo": {
            "name": "client", "version": "1"
        }}, "capabilities"),
        ({"protocolVersion": "2025-11-25", "capabilities": {}}, "clientInfo"),
    ):
        response = blueprint_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": f"missing-{missing_field}",
                "method": "initialize",
                "params": params,
            }
        )
        assert response["error"]["code"] == -32602
        assert missing_field in response["error"]["message"]


def test_stdio_is_binary_utf8_even_when_python_text_encoding_is_cp1252():
    method = "méthode/不可"
    request = {"jsonrpc": "2.0", "id": 37, "method": method}
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "0"
    environment["PYTHONIOENCODING"] = "cp1252"

    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "blueprint_mcp.py")],
        input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    decoded = completed.stdout.decode("utf-8")
    response = json.loads(decoded)
    assert response["error"]["code"] == -32601
    assert method in response["error"]["message"]


def test_tool_call_requires_explicit_scoped_config(monkeypatch):
    monkeypatch.delenv("BLUEPRINT_CONFIG", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_URL", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_KEY", raising=False)
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}},
        }
    )
    payload, is_error = _tool_payload(response)
    assert is_error is True
    assert "requires an explicit" in payload["error"]


def test_mcp_refuses_ambient_admin_credentials(monkeypatch):
    monkeypatch.setenv("BLUEPRINT_API_URL", "https://blueprint.example")
    monkeypatch.setenv("BLUEPRINT_API_KEY", "legacy-admin-key")
    monkeypatch.setattr(
        blueprint_mcp,
        "load_config",
        lambda: {
            "api_url": "https://blueprint.example",
            "api_key": "legacy-admin-key",
        },
    )
    calls = []
    monkeypatch.setattr(blueprint_mcp, "api", lambda *args, **kwargs: calls.append(args))

    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}},
        }
    )
    payload, is_error = _tool_payload(response)
    assert is_error is True
    assert "not a scoped ServiceCredential" in payload["error"]
    assert calls == []


def test_get_task_also_refuses_admin_credentials_before_backend_read(monkeypatch):
    monkeypatch.setenv("BLUEPRINT_API_URL", "https://blueprint.example")
    monkeypatch.setenv("BLUEPRINT_API_KEY", "legacy-admin-key")
    monkeypatch.setattr(
        blueprint_mcp,
        "load_config",
        lambda: {
            "api_url": "https://blueprint.example",
            "api_key": "legacy-admin-key",
        },
    )
    calls = []
    monkeypatch.setattr(blueprint_mcp, "api", lambda *args, **kwargs: calls.append(args))

    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "get_task", "arguments": {"task_id": TASK_ID}},
        }
    )
    payload, is_error = _tool_payload(response)
    assert is_error is True
    assert "not a scoped ServiceCredential" in payload["error"]
    assert calls == []


def test_list_projects_uses_the_scoped_http_seam(monkeypatch):
    _configure_scoped_agent(monkeypatch)
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/api/agent-access/v1/me":
            return _profile()
        raise AssertionError(path)

    monkeypatch.setattr(blueprint_mcp, "api", fake_api)
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {
                **_modern_meta(),
                "name": "list_projects",
                "arguments": {},
            },
        }
    )
    payload, is_error = _tool_payload(response)
    assert is_error is False
    assert response["result"]["resultType"] == "complete"
    assert response["result"]["_meta"][blueprint_mcp.SERVER_INFO_META_KEY]["name"] == (
        "blueprint"
    )
    assert payload["projects"] == _profile()["projects"]
    assert payload["returned"] == 1
    assert payload["has_more"] is False
    assert "projects" not in payload["access"]
    assert calls == [("GET", "/api/agent-access/v1/me", None)]


def test_list_projects_pages_and_bounds_profile_fields(monkeypatch):
    _configure_scoped_agent(monkeypatch)
    profile = _profile()
    profile["projects"] = [
        {
            "id": f"project-{index}",
            "name": "\x01" * 2_000,
            "github_owner": "owner",
            "github_repo": f"repo-{index}",
            "role": "viewer",
        }
        for index in range(55)
    ]
    def fake_api(method, path, body=None, timeout=30):
        assert method == "GET"
        assert "limit=10" in path
        assert "offset=5" in path
        page = dict(profile)
        page["projects"] = profile["projects"][5:15]
        page.update(
            total=55,
            limit=10,
            offset=5,
            returned=10,
            has_more=True,
            next_offset=15,
            truncation={"source_page": True},
            complete=False,
        )
        return page

    monkeypatch.setattr(blueprint_mcp, "api", fake_api)

    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                **_modern_meta(),
                "name": "list_projects",
                "arguments": {"limit": 10, "offset": 5},
            },
        }
    )
    payload, is_error = _tool_payload(response)

    assert is_error is False
    assert payload["total"] == 55
    assert payload["returned"] == 10
    assert payload["next_offset"] == 15
    assert payload["complete"] is False
    assert payload["truncation"]["project_fields"] is True
    assert all(
        len(json.dumps(project["name"], ensure_ascii=False).encode("utf-8"))
        <= blueprint_mcp.PROJECT_FIELD_MAX_SERIALIZED_BYTES
        for project in payload["projects"]
    )


def test_project_selector_and_error_catalog_are_bounded(monkeypatch):
    _configure_scoped_agent(monkeypatch)
    profile = _profile()
    profile["projects"] = [
        {
            "id": f"project-{index}",
            "name": "\x01" * 2_000,
            "github_owner": "owner",
            "github_repo": f"repo-{index}",
            "role": "viewer",
        }
        for index in range(80)
    ]
    profile.update(total=80, has_more=True, next_offset=50)
    monkeypatch.setattr(blueprint_mcp, "api", lambda *args, **kwargs: profile)

    too_long = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 24,
            "method": "tools/call",
            "params": {
                "name": "get_project_snapshot",
                "arguments": {"project": "x" * 201},
            },
        }
    )
    payload, is_error = _tool_payload(too_long)
    assert is_error is True
    assert "at most 200 characters" in payload["error"]

    missing = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 25,
            "method": "tools/call",
            "params": {
                "name": "get_project_snapshot",
                "arguments": {"project": "missing"},
            },
        }
    )
    payload, is_error = _tool_payload(missing)
    assert is_error is True
    assert "80 authorized projects" in payload["error"]
    assert "page list_projects and pass project_id" in payload["error"]
    assert len(json.dumps(missing, ensure_ascii=False).encode("utf-8")) < 10_000


def test_project_snapshot_is_one_versioned_server_read(monkeypatch):
    _configure_scoped_agent(monkeypatch)
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/api/agent-access/v1/me":
            return _profile()
        if path == f"/api/agent-access/v1/projects/{PROJECT_ID}/snapshot":
            return {
                "schema_version": "blueprint.project-snapshot.v1",
                "generated_at": "2026-08-10T12:00:00+00:00",
                "complete": False,
                "truncation": {"diagnostics": True, "response_bytes": False},
                "project": {"id": PROJECT_ID, "name": "Blueprint"},
                "attention": {"level": "clear", "items": []},
                "diagnostics": {"stuck_tasks": []},
                "resources": {"runners": {"online": 2, "total": 2}},
                "message_counts": {"needs_operator": 0},
                "read_state": {"tracked": False},
            }
        raise AssertionError(path)

    monkeypatch.setattr(blueprint_mcp, "api", fake_api)
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_project_snapshot",
                "arguments": {"project": "Blueprint"},
            },
        }
    )
    payload, is_error = _tool_payload(response)
    assert is_error is False
    assert payload["schema_version"] == "blueprint.project-snapshot.v1"
    assert payload["complete"] is False
    assert payload["truncation"]["diagnostics"] is True
    assert payload["project"]["id"] == PROJECT_ID
    assert "access" not in payload
    assert calls == [
        ("GET", "/api/agent-access/v1/me", None),
        (
            "GET",
            f"/api/agent-access/v1/projects/{PROJECT_ID}/snapshot",
            None,
        ),
    ]


def test_task_and_message_tools_use_bounded_membership_scoped_routes(monkeypatch):
    _configure_scoped_agent(monkeypatch)
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/api/agent-access/v1/me":
            return _profile()
        if path.startswith("/api/agent-access/v1/tasks?"):
            return {
                "schema_version": "blueprint.agent-tasks.v1",
                "complete": True,
                "tasks": [{"id": TASK_ID}],
                "total": 1,
            }
        if path == f"/api/agent-access/v1/tasks/{TASK_ID}":
            return {
                "schema_version": "blueprint.agent-task.v1",
                "complete": True,
                "task": {"id": TASK_ID, "project_id": PROJECT_ID},
            }
        if path.startswith("/api/agent-access/v1/messages?"):
            return {
                "schema_version": "blueprint.agent-messages.v1",
                "complete": True,
                "messages": [],
                "total": 0,
            }
        raise AssertionError(path)

    monkeypatch.setattr(blueprint_mcp, "api", fake_api)
    task_list_response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "list_tasks",
                "arguments": {
                    "project_id": PROJECT_ID,
                    "statuses": ["FAILED", "BLOCKED"],
                    "limit": 25,
                    "offset": 50,
                },
            },
        }
    )
    task_list, is_error = _tool_payload(task_list_response)
    assert is_error is False
    assert task_list["schema_version"] == "blueprint.agent-tasks.v1"
    assert task_list["total"] == 1
    task_list_path = calls[-1][1]
    task_query = parse_qs(urlsplit(task_list_path).query)
    assert task_query == {
        "project_id": [PROJECT_ID],
        "limit": ["25"],
        "offset": ["50"],
        "status": ["FAILED", "BLOCKED"],
    }

    task_response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "get_task", "arguments": {"task_id": TASK_ID}},
        }
    )
    task, task_error = _tool_payload(task_response)
    assert task_error is False
    assert task["schema_version"] == "blueprint.agent-task.v1"
    assert task["task"]["id"] == TASK_ID

    messages_response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "list_messages",
                "arguments": {
                    "project": "Blueprint",
                    "status": "needs_action",
                    "limit": 10,
                    "offset": 30,
                },
            },
        }
    )
    messages, messages_error = _tool_payload(messages_response)
    assert messages_error is False
    assert messages["schema_version"] == "blueprint.agent-messages.v1"
    assert messages["messages"] == []
    message_query = parse_qs(urlsplit(calls[-1][1]).query)
    assert message_query == {
        "project_id": [PROJECT_ID],
        "status": ["needs_action"],
        "limit": ["10"],
        "offset": ["30"],
    }
    assert all(method == "GET" for method, _path, _body in calls)


def test_invalid_task_id_fails_before_any_backend_call(monkeypatch):
    _configure_scoped_agent(monkeypatch)
    calls = []
    monkeypatch.setattr(
        blueprint_mcp,
        "api",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                **_modern_meta(),
                "name": "get_task",
                "arguments": {"task_id": "../admin"},
            },
        }
    )
    payload, is_error = _tool_payload(response)
    assert is_error is True
    assert response["result"]["resultType"] == "complete"
    assert "valid UUID" in payload["error"]
    assert calls == []


def test_invalid_explicit_project_never_falls_back_to_default(monkeypatch):
    _configure_scoped_agent(monkeypatch)
    calls = []

    def fake_api(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/api/agent-access/v1/me":
            return _profile()
        raise AssertionError(path)

    monkeypatch.setattr(blueprint_mcp, "api", fake_api)
    response = blueprint_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "get_project_snapshot",
                "arguments": {"project_id": ""},
            },
        }
    )
    payload, is_error = _tool_payload(response)
    assert is_error is True
    assert "valid UUID" in payload["error"]
    assert calls == []
