import json

from app.work_session_commands import (
    MAX_WORK_SESSION_COMMANDS,
    build_work_session_commands,
)


def _commands_by_id(commands):
    return {command["id"]: command for command in commands}


def test_work_session_commands_convert_actions_to_navigation_descriptors():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=[
            {
                "id": "attention.message:message-1",
                "kind": "reply",
                "label": "Reply to Strategic question",
                "reason": "question text stays out of commands",
                "priority": "critical",
                "source": "attention",
                "target": {
                    "surface": "messages",
                    "project_id": "project-1",
                    "message_id": "message-1",
                    "action": "reply",
                    "secret": "drop-me",
                },
                "enabled": True,
            }
        ],
    )

    command = _commands_by_id(commands)["attention.message:message-1"]
    assert command == {
        "id": "attention.message:message-1",
        "label": "Reply to Strategic question",
        "group": "attention",
        "priority": "critical",
        "enabled": True,
        "target": {
            "surface": "messages",
            "project_id": "project-1",
            "message_id": "message-1",
            "action": "reply",
        },
        "refetch_surfaces": [],
    }
    assert "mutation" not in command


def test_work_session_commands_include_fixed_session_commands_and_canvas():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package={
            "canvas": {
                "id": "strategic-canvas:project-1",
                "title": "Strategic context map",
            },
            "resources": [
                {
                    "id": "strategic-canvas:project-1",
                    "kind": "canvas",
                    "target": {
                        "surface": "project",
                        "section": "problem-model",
                        "secret": "drop-me",
                    },
                }
            ],
        },
        actions=[],
    )

    by_id = _commands_by_id(commands)
    assert {
        "session.check_in",
        "session.mode.continuous",
        "session.mode.monitor",
        "session.mode.chat",
        "session.clear_selected_refs",
        "context.canvas.open",
    }.issubset(by_id)

    check_in = by_id["session.check_in"]
    assert "mutation" not in check_in
    assert check_in["refetch_surfaces"] == ["strategic_session"]

    workday = by_id["session.mode.continuous"]
    assert workday["label"] == "Workday mode"
    assert workday["enabled"] is True
    assert workday["mutation"] == {
        "method": "POST",
        "path": "/api/admin/projects/project-1/strategic-session",
        "body": {"workbench": {"interaction_mode": "continuous"}},
    }
    assert workday["target"]["surface"] == "messages"
    assert workday["refetch_surfaces"] == ["strategic_session", "runtime"]

    assert by_id["session.mode.chat"]["enabled"] is False
    assert by_id["session.clear_selected_refs"]["enabled"] is False
    assert by_id["context.canvas.open"]["target"] == {
        "surface": "project",
        "project_id": "project-1",
        "section": "problem-model",
    }
    assert "mutation" not in by_id["context.canvas.open"]


def test_work_session_commands_clear_selected_refs_uses_existing_session_endpoint():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={
            "interaction_mode": "monitor",
            "selected_refs": [
                {"kind": "task", "id": "task-1", "label": "Selected task"}
            ],
        },
        context_package=None,
        actions=[],
    )

    clear_refs = _commands_by_id(commands)["session.clear_selected_refs"]
    assert clear_refs["enabled"] is True
    assert clear_refs["target"] == {
        "surface": "project",
        "project_id": "project-1",
        "section": "selected_refs",
        "action": "clear",
    }
    assert clear_refs["mutation"] == {
        "method": "POST",
        "path": "/api/admin/projects/project-1/strategic-session",
        "body": {"workbench": {"selected_refs": []}},
    }
    assert clear_refs["refetch_surfaces"] == ["strategic_session"]


def test_work_session_command_mutations_url_encode_project_id_path_segment():
    commands = build_work_session_commands(
        project_id="owner/repo project",
        workbench_state={
            "interaction_mode": "monitor",
            "selected_refs": [
                {"kind": "task", "id": "task-1", "label": "Selected task"}
            ],
        },
        context_package=None,
        actions=[],
    )

    mutation_paths = [
        command["mutation"]["path"]
        for command in commands
        if "mutation" in command
    ]

    assert mutation_paths
    assert set(mutation_paths) == {
        "/api/admin/projects/owner%2Frepo%20project/strategic-session"
    }
    assert (
        "/api/admin/projects/owner/repo project/strategic-session"
        not in mutation_paths
    )


def test_work_session_commands_dedupe_and_bound_list():
    actions = [
        {
            "id": "session.check_in",
            "label": "Duplicate action should not replace fixed command",
            "priority": "critical",
            "source": "attention",
            "target": {"surface": "messages", "message_id": "duplicate"},
            "enabled": True,
        },
        *[
            {
                "id": f"attention.message:{index}",
                "label": f"Reply {index}",
                "priority": "normal",
                "source": "attention",
                "target": {"surface": "messages", "message_id": f"message-{index}"},
                "enabled": True,
            }
            for index in range(40)
        ],
    ]

    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=actions,
    )

    ids = [command["id"] for command in commands]
    assert len(commands) == MAX_WORK_SESSION_COMMANDS
    assert len(ids) == len(set(ids))
    assert _commands_by_id(commands)["session.check_in"]["label"] == "Check in"


def test_work_session_commands_do_not_expose_raw_secret_payload_or_context():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={
            "interaction_mode": "monitor",
            "selected_refs": [
                {"kind": "task", "id": "task-1", "label": "Selected task"}
            ],
        },
        context_package={
            "canvas": {
                "id": "strategic-canvas:project-1",
                "title": "Strategic context map",
            },
            "resources": [
                {
                    "id": "strategic-canvas:project-1",
                    "kind": "canvas",
                    "target": {
                        "surface": "project",
                        "section": "problem-model",
                        "raw_context": "raw-secret-context",
                    },
                }
            ],
        },
        actions=[
            {
                "id": "attention.task:task-1",
                "label": "Inspect task",
                "priority": "high",
                "source": "attention",
                "reason": "secret-token should not be copied",
                "payload": {"command": "run-with-secret-token"},
                "target": {
                    "surface": "task",
                    "task_id": "task-1",
                    "secret": "secret-token",
                    "raw_context": "raw-secret-context",
                },
                "enabled": True,
            }
        ],
    )

    serialized = json.dumps(commands, sort_keys=True)
    assert "secret-token" not in serialized
    assert "raw-secret-context" not in serialized
    assert "run-with-secret-token" not in serialized
    assert "payload" not in serialized
