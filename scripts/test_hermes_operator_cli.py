"""Focused CLI contract tests for the Hermes project-operator profile.

These tests intentionally exercise the real argparse/main dispatch boundary while
mocking the HTTP transport.  A scoped credential may inspect its own ``/me``
profile, but denied commands must stop before project resolution, handler work,
filesystem access, or subprocess execution.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib

import blueprint
import blueprint_api
import pytest


PROJECT_ID = "11111111-1111-4111-8111-111111111111"
TASK_ID = "22222222-2222-4222-8222-222222222222"
MESSAGE_ID = "33333333-3333-4333-8333-333333333333"
DIRECTION_ID = "d-hermes-1"
CRITERION_KEY = "c-hermes-1"

# The catalog ``validate_project_id`` checks ``--project-id`` against. Every
# test here passes ``--project-id PROJECT_ID``, so the id has to be present or
# ``main()`` exits 1 before reaching the command under test.
PROJECT_CATALOG = [
    {
        "id": PROJECT_ID,
        "name": "hermes/scoped-project",
        "github_owner": "hermes",
        "github_repo": "scoped-project",
        "role": "member",
    }
]


def _install_double(monkeypatch, name: str, value) -> None:
    """Install a test double on BOTH bindings of a ``blueprint_api`` name.

    ``blueprint.py`` does ``from blueprint_api import (api, get_project_id,
    list_projects, load_config, validate_project_id, ...)``, so every one of
    those names exists twice: once in ``blueprint`` and once in
    ``blueprint_api``. Patching only ``blueprint`` leaves the calls that
    ``blueprint_api`` makes *internally* bound to the real functions --
    ``main()`` -> ``validate_project_id()`` -> ``list_projects()`` ->
    ``load_config()``/``api()`` never saw the mock, resolved the operator's
    ambient ``~/.blueprint-config.json``, and issued live authenticated
    requests to the production orchestrator (21 of them from this file alone).
    CI containers have no config file, so the mock hole was invisible there.

    Patch the source of truth as well, so a double that is supposed to
    intercept actually does.
    """
    monkeypatch.setattr(blueprint_api, name, value)
    monkeypatch.setattr(blueprint, name, value)


@pytest.fixture(autouse=True)
def _clear_scoped_profile_cache(monkeypatch):
    monkeypatch.setattr(blueprint, "_SCOPED_PROFILE_CACHE", None)


def _install_scoped_cli(
    monkeypatch,
    *,
    scopes: tuple[str, ...] = ("project-operator",),
    responder: Callable[[str, str, dict | None], dict] | None = None,
) -> list[tuple[str, str, dict | None]]:
    calls: list[tuple[str, str, dict | None]] = []

    _install_double(
        monkeypatch,
        "load_config",
        lambda: {"api_key": "bpsc_hermes-test", "default_repo": "ignored/repo"},
    )
    _install_double(
        monkeypatch,
        "get_project_id",
        lambda project_id=None: project_id or PROJECT_ID,
    )
    _install_double(monkeypatch, "list_projects", lambda: list(PROJECT_CATALOG))

    def fake_api(method: str, path: str, body: dict | None = None, **_kwargs):
        calls.append((method, path, body))
        if path == "/api/agent-access/v1/me":
            return {
                "credential": {"name": "hermes", "scopes": list(scopes)},
                "projects": [{"id": PROJECT_ID, "role": "member"}],
            }
        if responder is not None:
            return responder(method, path, body)
        return {}

    _install_double(monkeypatch, "api", fake_api)
    return calls


def _main_json(*argv: str) -> int:
    return blueprint.main(["--json", "--project-id", PROJECT_ID, *argv])


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("deploy", ["deploy", "--yes"]),
        ("migrate", ["migrate", "--local"]),
        ("compute", ["compute", "run", "echo should-not-run"]),
        ("settings", ["settings", "show"]),
        ("fleet", ["fleet", "pool"]),
        ("tasks", ["tasks", "cancel-all", "--yes"]),
        ("strategic", ["strategic", "stop", "--yes"]),
        ("agent-access", ["agent-access", "grant", "forbidden", "--yes"]),
        ("messages", ["messages", "confirm", "list"]),
        ("ops", ["ops", "repair", "--yes"]),
    ],
)
def test_project_operator_denies_unsupported_command_before_local_work(
    monkeypatch, capsys, command, argv
):
    calls = _install_scoped_cli(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("denied scoped command crossed the global CLI gate")

    monkeypatch.setitem(blueprint._DISPATCH, command, forbidden)
    monkeypatch.setattr(blueprint, "resolve_project_id_by_name", forbidden)
    monkeypatch.setattr(blueprint, "Path", forbidden)
    monkeypatch.setattr(blueprint.subprocess, "run", forbidden)
    monkeypatch.setattr(blueprint.subprocess, "Popen", forbidden)

    result = blueprint.main(["--project", "Blueprint", *argv])

    assert result == 1
    assert calls == [("GET", "/api/agent-access/v1/me", None)]
    error = capsys.readouterr().err
    assert "scoped ServiceCredential command not permitted" in error
    assert "platform admin" in error


def test_coding_agent_scope_cannot_use_project_operator_write(monkeypatch, capsys):
    calls = _install_scoped_cli(monkeypatch, scopes=("coding-agent:read",))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("coding-agent write reached project resolution")

    monkeypatch.setattr(blueprint, "get_project_id", forbidden)

    result = blueprint.main(
        ["tasks", "requeue", TASK_ID, "--guidance", "Retry safely", "--yes"]
    )

    assert result == 1
    assert calls == [("GET", "/api/agent-access/v1/me", None)]
    assert "scoped ServiceCredential command not permitted" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected_calls"),
    [
        (
            ["tasks", "requeue", TASK_ID, "--guidance", "Retry with logs", "--yes"],
            [
                (
                    "POST",
                    f"/api/operator-access/v1/tasks/{TASK_ID}/requeue",
                    {"guidance": "Retry with logs"},
                )
            ],
        ),
        (
            [
                "tasks",
                "request-changes",
                TASK_ID,
                "Add the missing isolation test",
                "--action",
                "review",
                "--yes",
            ],
            [
                (
                    "POST",
                    f"/api/operator-access/v1/tasks/{TASK_ID}/request-changes",
                    {
                        "feedback": "Add the missing isolation test",
                        "action": "review",
                    },
                )
            ],
        ),
        (
            [
                "tasks",
                "cancel",
                TASK_ID,
                "--reason",
                "Superseded by the bounded follow-up",
                "--yes",
            ],
            [
                (
                    "GET",
                    f"/api/operator-access/v1/tasks/{TASK_ID}/cancel-impact",
                    None,
                ),
                (
                    "POST",
                    f"/api/operator-access/v1/tasks/{TASK_ID}/cancel",
                    {
                        "confirm_cascade": False,
                        "reason": "Superseded by the bounded follow-up",
                    },
                ),
            ],
        ),
        (
            [
                "tasks",
                "create",
                "Add operator regression test",
                "Implement the bounded regression coverage",
                "--acceptance-behavior",
                "The focused operator CLI tests pass",
                "--idempotency-key",
                "hermes-create-1",
                "--base-branch",
                "automain",
                "--require-deps-landed-on",
                "automain",
                "--release-base-branch",
                "automain",
                "--yes",
            ],
            [
                ("GET", "/api/operator-access/v1/task-capabilities", None),
                (
                    "POST",
                    f"/api/operator-access/v1/projects/{PROJECT_ID}/tasks",
                    {
                        "title": "Add operator regression test",
                        "prompt": "Implement the bounded regression coverage",
                        "task_type": "create",
                        "idempotency_key": "hermes-create-1",
                        "base_branch": "automain",
                        "require_deps_landed_on": "automain",
                        "release_base_branch": "automain",
                        "acceptance_evidence": {
                            "kind": "observable_behavior",
                            "description": "The focused operator CLI tests pass",
                        },
                    },
                ),
            ],
        ),
    ],
)
def test_project_operator_uses_exact_task_routes_and_bodies(
    monkeypatch, argv, expected_calls
):
    def responder(_method: str, path: str, _body: dict | None) -> dict:
        if path == "/api/operator-access/v1/task-capabilities":
            return {
                "task_capabilities": {
                    "create": {
                        "user_launchable": True,
                        "requires_acceptance_evidence": True,
                    }
                }
            }
        if path.endswith("/cancel-impact"):
            return {
                "active_child_cascade_count": 0,
                "waiting_dependent_count": 0,
            }
        return {}

    calls = _install_scoped_cli(monkeypatch, responder=responder)

    assert _main_json(*argv) == 0
    assert calls[1:] == expected_calls


def test_project_operator_criterion_rewrite_uses_exact_cas_body_and_receipt(
    monkeypatch
):
    expected_text = "Old exact criterion"
    replacement_text = "Corrected exact criterion"

    def responder(_method: str, path: str, body: dict | None) -> dict:
        if path.endswith(f"/acceptance-criteria/{CRITERION_KEY}/text"):
            assert body is not None
            return {
                "status": "ok",
                "criterion_key": CRITERION_KEY,
                "text": body["text"],
                "changed": True,
                "compare_state": "expected",
                "expected_text_sha256": hashlib.sha256(
                    body["expected_text"].encode("utf-8")
                ).hexdigest(),
                "previous_text_sha256": hashlib.sha256(
                    body["expected_text"].encode("utf-8")
                ).hexdigest(),
                "text_sha256": hashlib.sha256(body["text"].encode("utf-8")).hexdigest(),
                "release_version_id": "44444444-4444-4444-8444-444444444444",
                "lineage": {
                    "criterion_id": "55555555-5555-4555-8555-555555555555",
                    "created_by": "strategist",
                    "created_at": "2026-08-28T00:00:00Z",
                },
            }
        return {}

    calls = _install_scoped_cli(monkeypatch, responder=responder)

    assert (
        _main_json(
            "criteria",
            "rewrite",
            CRITERION_KEY,
            "--expected-text",
            expected_text,
            "--text",
            replacement_text,
            "--yes",
        )
        == 0
    )
    assert calls[1:] == [
        (
            "PATCH",
            f"/api/operator-access/v1/projects/{PROJECT_ID}/acceptance-criteria/"
            f"{CRITERION_KEY}/text",
            {"expected_text": expected_text, "text": replacement_text},
        )
    ]


def test_project_operator_criterion_rewrite_preserves_raw_file_text(
    monkeypatch, tmp_path
):
    expected_path = tmp_path / "expected.txt"
    replacement_path = tmp_path / "replacement.txt"
    expected_path.write_text("Old text\nwith exact newline", encoding="utf-8")
    replacement_path.write_text("New text\nwith exact newline", encoding="utf-8")

    def responder(_method: str, path: str, body: dict | None) -> dict:
        assert body is not None
        return {
            "status": "ok",
            "criterion_key": CRITERION_KEY,
            "text": body["text"],
            "changed": True,
            "compare_state": "expected",
            "expected_text_sha256": hashlib.sha256(
                body["expected_text"].encode("utf-8")
            ).hexdigest(),
            "previous_text_sha256": hashlib.sha256(
                body["expected_text"].encode("utf-8")
            ).hexdigest(),
            "text_sha256": hashlib.sha256(body["text"].encode("utf-8")).hexdigest(),
            "release_version_id": "44444444-4444-4444-8444-444444444444",
            "lineage": {"criterion_id": "55555555-5555-4555-8555-555555555555"},
        }

    calls = _install_scoped_cli(monkeypatch, responder=responder)

    assert (
        _main_json(
            "criteria",
            "rewrite",
            CRITERION_KEY,
            "--expected-text-file",
            str(expected_path),
            "--text-file",
            str(replacement_path),
            "--yes",
        )
        == 0
    )
    assert calls[1:][0][2] == {
        "expected_text": "Old text\nwith exact newline",
        "text": "New text\nwith exact newline",
    }


def test_project_operator_criterion_rewrite_fails_ambiguous_readback(
    monkeypatch, capsys
):
    calls = _install_scoped_cli(monkeypatch, responder=lambda *_args: {"status": "ok"})

    result = _main_json(
        "criteria",
        "rewrite",
        CRITERION_KEY,
        "--expected-text",
        "Old text",
        "--text",
        "New text",
        "--yes",
    )

    assert result == 1
    assert calls[1:][0][0] == "PATCH"
    assert "ambiguous or contradictory receipt" in capsys.readouterr().err


def test_project_operator_requeue_requires_explicit_guidance(monkeypatch, capsys):
    calls = _install_scoped_cli(monkeypatch)

    result = _main_json("tasks", "requeue", TASK_ID, "--yes")

    assert result == 1
    assert calls == [("GET", "/api/agent-access/v1/me", None)]
    assert "requires non-empty --guidance" in capsys.readouterr().err


def test_project_operator_cancel_requires_reported_cascade_acknowledgement(
    monkeypatch, capsys
):
    def responder(_method: str, path: str, _body: dict | None) -> dict:
        if path.endswith("/cancel-impact"):
            return {
                "active_child_cascade_count": 2,
                "waiting_dependent_count": 1,
            }
        return {}

    calls = _install_scoped_cli(monkeypatch, responder=responder)

    result = _main_json("tasks", "cancel", TASK_ID, "--yes")

    assert result == 1
    assert calls[1:] == [
        (
            "GET",
            f"/api/operator-access/v1/tasks/{TASK_ID}/cancel-impact",
            None,
        )
    ]
    assert "--confirm-cascade" in capsys.readouterr().err


def test_project_operator_cancel_sends_explicit_cascade_acknowledgement(monkeypatch):
    def responder(_method: str, path: str, _body: dict | None) -> dict:
        if path.endswith("/cancel-impact"):
            return {
                "active_child_cascade_count": 1,
                "waiting_dependent_count": 0,
            }
        return {}

    calls = _install_scoped_cli(monkeypatch, responder=responder)

    assert (
        _main_json(
            "tasks", "cancel", TASK_ID, "--confirm-cascade", "--yes"
        )
        == 0
    )
    assert calls[1:] == [
        (
            "GET",
            f"/api/operator-access/v1/tasks/{TASK_ID}/cancel-impact",
            None,
        ),
        (
            "POST",
            f"/api/operator-access/v1/tasks/{TASK_ID}/cancel",
            {"confirm_cascade": True},
        ),
    ]


@pytest.mark.parametrize(
    ("argv", "expected_calls"),
    [
        (
            [
                "directions",
                "add",
                "Investigate the bounded operator surface",
                "--priority",
                "high",
                "--paths",
                "scripts/blueprint.py",
                "--completion-criteria",
                "Hermes can steer safely",
                "--staged",
                "--idempotency-key",
                "hermes-direction-1",
                "--yes",
            ],
            [
                (
                    "POST",
                    f"/api/operator-access/v1/projects/{PROJECT_ID}/directions",
                    {
                        "text": "Investigate the bounded operator surface",
                        "priority": "high",
                        "paths": ["scripts/blueprint.py"],
                        "completion_criteria": "Hermes can steer safely",
                        "staged": True,
                        "idempotency_key": "hermes-direction-1",
                    },
                )
            ],
        ),
        (
            [
                "directions",
                "update",
                DIRECTION_ID,
                "--text",
                "Refined bounded direction",
                "--priority",
                "low",
                "--paths",
                "scripts/blueprint.py",
                "backend/app/routers/operator_access.py",
                "--completion-criteria",
                "All focused tests pass",
                "--yes",
            ],
            [
                (
                    "GET",
                    f"/api/agent-access/v1/directions?project_id={PROJECT_ID}&limit=100&offset=0",
                    None,
                ),
                (
                    "PATCH",
                    f"/api/operator-access/v1/projects/{PROJECT_ID}/directions/{DIRECTION_ID}",
                    {
                        "text": "Refined bounded direction",
                        "priority": "low",
                        "paths": [
                            "scripts/blueprint.py",
                            "backend/app/routers/operator_access.py",
                        ],
                        "completion_criteria": "All focused tests pass",
                    },
                ),
            ],
        ),
        (
            ["directions", "retarget", DIRECTION_ID, "7", "--yes"],
            [
                (
                    "GET",
                    f"/api/agent-access/v1/directions?project_id={PROJECT_ID}&limit=100&offset=0",
                    None,
                ),
                (
                    "POST",
                    f"/api/operator-access/v1/projects/{PROJECT_ID}/directions/"
                    f"{DIRECTION_ID}/retarget",
                    {"version": 7},
                ),
            ],
        ),
        (
            ["directions", "activate", DIRECTION_ID, "--yes"],
            [
                (
                    "GET",
                    f"/api/agent-access/v1/directions?project_id={PROJECT_ID}&limit=100&offset=0",
                    None,
                ),
                (
                    "POST",
                    f"/api/operator-access/v1/projects/{PROJECT_ID}/directions/"
                    f"{DIRECTION_ID}/activate",
                    {},
                ),
            ],
        ),
        (
            ["directions", "cancel", DIRECTION_ID, "--yes"],
            [
                (
                    "GET",
                    f"/api/agent-access/v1/directions?project_id={PROJECT_ID}&limit=100&offset=0",
                    None,
                ),
                (
                    "POST",
                    f"/api/operator-access/v1/projects/{PROJECT_ID}/directions/"
                    f"{DIRECTION_ID}/cancel",
                    {},
                ),
            ],
        ),
    ],
)
def test_project_operator_uses_exact_direction_routes_and_bodies(
    monkeypatch, argv, expected_calls
):
    def responder(_method: str, path: str, _body: dict | None) -> dict:
        if path.startswith("/api/agent-access/v1/directions?"):
            return {
                "directions": [
                    {"id": DIRECTION_ID, "status": "staged", "text": "Original"}
                ]
            }
        return {}

    calls = _install_scoped_cli(monkeypatch, responder=responder)

    assert _main_json(*argv) == 0
    assert calls[1:] == expected_calls


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (
            [
                "messages",
                "send",
                "Please inspect the blocked task",
                "--type",
                "BLOCKER",
                "--idempotency-key",
                "hermes-message-1",
            ],
            (
                "POST",
                f"/api/operator-access/v1/projects/{PROJECT_ID}/messages",
                {
                    "content": "Please inspect the blocked task",
                    "message_type": "BLOCKER",
                    "idempotency_key": "hermes-message-1",
                },
            ),
        ),
        (
            ["messages", "answer", MESSAGE_ID, "Use the existing project"],
            (
                "POST",
                f"/api/operator-access/v1/projects/{PROJECT_ID}/messages/{MESSAGE_ID}/answer",
                {"resolution": "Use the existing project"},
            ),
        ),
        (
            ["strategic", "start"],
            (
                "POST",
                f"/api/operator-access/v1/projects/{PROJECT_ID}/strategic/start",
                {},
            ),
        ),
        (
            [
                "strategic",
                "send",
                "Continue from the latest project context",
                "--type",
                "QUESTION",
                "--idempotency-key",
                "hermes-strategic-1",
            ],
            (
                "POST",
                f"/api/operator-access/v1/projects/{PROJECT_ID}/strategic/messages",
                {
                    "content": "Continue from the latest project context",
                    "message_type": "QUESTION",
                    "idempotency_key": "hermes-strategic-1",
                },
            ),
        ),
        (
            ["strategic-cycle", "--phase", "strategic_review", "--yes"],
            (
                "POST",
                f"/api/operator-access/v1/projects/{PROJECT_ID}/strategic-cycle",
                {"phase": "strategic_review"},
            ),
        ),
    ],
)
def test_project_operator_uses_exact_message_and_strategic_contracts(
    monkeypatch, argv, expected_call
):
    calls = _install_scoped_cli(monkeypatch)

    assert _main_json(*argv) == 0
    assert calls[1:] == [expected_call]


def test_project_operator_strategic_chat_uses_bounded_conversation_and_message(
    monkeypatch
):
    inputs = iter(["Continue the current strategy", "/quit"])

    def responder(_method: str, path: str, _body: dict | None) -> dict:
        if path.endswith("/strategic-conversation?limit=50"):
            return {"data": {"items": []}}
        if path.endswith("/strategic/messages"):
            return {"id": MESSAGE_ID}
        return {}

    calls = _install_scoped_cli(monkeypatch, responder=responder)
    monkeypatch.setattr(
        blueprint, "input", lambda _prompt: next(inputs), raising=False
    )
    monkeypatch.setattr(blueprint.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        blueprint.uuid,
        "uuid4",
        lambda: "00000000-0000-4000-8000-000000000001",
    )

    assert (
        _main_json(
            "strategic", "chat", "--interval", "0.5", "--timeout", "0.5"
        )
        == 0
    )
    conversation_route = (
        f"/api/operator-access/v1/projects/{PROJECT_ID}/"
        "strategic-conversation?limit=50"
    )
    assert calls[1:] == [
        ("GET", conversation_route, None),
        (
            "POST",
            f"/api/operator-access/v1/projects/{PROJECT_ID}/strategic/messages",
            {
                "content": "Continue the current strategy",
                "message_type": "NOTE",
                "idempotency_key": (
                    "strategic-chat:00000000-0000-4000-8000-000000000001"
                ),
            },
        ),
        ("GET", conversation_route, None),
    ]


@pytest.mark.parametrize("command", ["status", "diagnostics"])
def test_scoped_status_and_diagnostics_use_project_snapshot(monkeypatch, command):
    calls = _install_scoped_cli(
        monkeypatch,
        responder=lambda _method, _path, _body: {
            "schema_version": "blueprint.agent-snapshot.v1",
            "project": {"id": PROJECT_ID},
            "attention": {},
            "diagnostics": {},
            "resources": {},
        },
    )

    assert _main_json(command) == 0
    assert calls[1:] == [
        (
            "GET",
            f"/api/agent-access/v1/projects/{PROJECT_ID}/snapshot",
            None,
        )
    ]


@pytest.mark.parametrize(
    "unsafe_args",
    [
        ["--repo", "owner/repo"],
        ["--model", "provider-model"],
        ["--effort", "high"],
        ["--hotfix"],
        ["--acceptance-evidence-file", "local-secret.json"],
    ],
)
def test_project_operator_rejects_unsafe_task_create_flags_before_resolution(
    monkeypatch, capsys, unsafe_args
):
    calls = _install_scoped_cli(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe create flag reached project or filesystem access")

    monkeypatch.setattr(blueprint, "get_project_id", forbidden)
    monkeypatch.setattr(blueprint, "Path", forbidden)

    result = blueprint.main(
        [
            "tasks",
            "create",
            "Unsafe authority",
            "This must be rejected",
            "--yes",
            *unsafe_args,
        ]
    )

    assert result == 1
    assert calls == [("GET", "/api/agent-access/v1/me", None)]
    assert "does not authorize" in capsys.readouterr().err


@pytest.mark.parametrize(
    "unsafe_args",
    [
        ["--status", "paused"],
        ["--staged-for-version", "9"],
        ["--reset-context"],
        ["--reset-history"],
        ["--reset-agent-notes"],
        ["--reset-all"],
    ],
)
def test_project_operator_rejects_unsafe_direction_update_before_resolution(
    monkeypatch, capsys, unsafe_args
):
    calls = _install_scoped_cli(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe direction update reached project resolution")

    monkeypatch.setattr(blueprint, "get_project_id", forbidden)

    result = blueprint.main(
        ["directions", "update", DIRECTION_ID, "--yes", *unsafe_args]
    )

    assert result == 1
    assert calls == [("GET", "/api/agent-access/v1/me", None)]
    assert "permits only text, priority, paths, and completion criteria" in (
        capsys.readouterr().err
    )


def test_project_operator_rejects_strategic_runner_override(monkeypatch, capsys):
    calls = _install_scoped_cli(monkeypatch)

    result = _main_json("strategic", "start", "--runner-id", "runner-secret")

    assert result == 1
    assert calls == [("GET", "/api/agent-access/v1/me", None)]
    assert "server-selected placement" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["agent-access", "grant", "--help"], "project-operator"),
        (["tasks", "request-changes", "--help"], "--action"),
        (["tasks", "cancel", "--help"], "--confirm-cascade"),
        (["messages", "send", "--help"], "--idempotency-key"),
        (["strategic", "send", "--help"], "--idempotency-key"),
    ],
)
def test_operator_parser_help_exposes_bounded_surface(capsys, argv, expected):
    parser = blueprint.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)

    assert exc.value.code == 0
    assert expected in capsys.readouterr().out


def test_operator_grant_renderer_points_to_writable_operator_config(capsys):
    blueprint._render_agent_credential_grant(
        {
            "name": "hermes",
            "id": "credential-1",
            "token": "bpsc_shown-once",
            "scopes": ["project-operator"],
        }
    )

    rendered = capsys.readouterr().out
    assert "BLUEPRINT_CONFIG" in rendered
    assert "HERMES_OPERATOR_ACCESS.md" in rendered
    assert "read-only config" not in rendered
    assert "BLUEPRINT_AGENT_CONFIG" not in rendered
