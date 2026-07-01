import json
import uuid

from app.work_session_commands import (
    MAX_WORK_SESSION_COMMANDS,
    build_work_session_commands,
)
from app.workbench_contracts import (
    COMMAND_ID_ATTENTION_MESSAGES_MARK_SEEN,
    COMMAND_ID_SESSION_CLEAR_SELECTED_REFS,
    COMMAND_ID_SESSION_RUNNER_CLEAR_PREFERRED,
    alert_dismiss_command_id,
    executable_work_session_command_contract,
    runner_preference_command_id,
    session_mode_command_id,
    work_session_command_kind,
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
        "executable": False,
    }
    assert "mutation" not in command


def test_work_session_commands_validate_action_target_vocabulary():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=[
            {
                "id": "attention.debug:raw",
                "kind": "inspect",
                "label": "Open raw target",
                "priority": "normal",
                "source": "attention",
                "target": {
                    "surface": "debug",
                    "section": "raw",
                    "task_id": "task-1",
                },
                "enabled": True,
            },
            {
                "id": "attention.runtime:runner",
                "kind": "inspect",
                "label": "Open runner",
                "priority": "normal",
                "source": "attention",
                "target": {
                    "surface": "runtime",
                    "section": "debug-panel",
                    "runner_id": "runner-1",
                },
                "enabled": True,
            },
            {
                "id": "attention.runtime:legacy-runner",
                "kind": "inspect",
                "label": "Open legacy runner",
                "priority": "normal",
                "source": "attention",
                "target": {
                    "surface": "runtime",
                    "section": "runners",
                    "runner_id": "runner-2",
                },
                "enabled": True,
            },
        ],
    )

    by_id = _commands_by_id(commands)
    assert "attention.debug:raw" not in by_id
    assert by_id["attention.runtime:runner"]["target"] == {
        "surface": "runtime",
        "runner_id": "runner-1",
    }
    assert by_id["attention.runtime:legacy-runner"]["target"] == {
        "surface": "runtime",
        "section": "devices",
        "runner_id": "runner-2",
    }


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
        "session.mode.check_in",
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

    check_in_mode = by_id["session.mode.check_in"]
    assert check_in_mode["label"] == "Check-in mode"
    assert check_in_mode["enabled"] is True
    assert check_in_mode["mutation"] == {
        "method": "POST",
        "path": "/api/admin/projects/project-1/strategic-session",
        "body": {"workbench": {"interaction_mode": "check_in"}},
    }
    assert check_in_mode["target"]["action"] == "set_check_in_mode"
    assert check_in_mode["refetch_surfaces"] == ["strategic_session"]

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


def test_work_session_commands_include_runner_preference_mutations():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "continuous", "selected_refs": []},
        context_package=None,
        actions=[],
        local_runner={
            "runners": [
                {
                    "id": "runner-1",
                    "hostname": "desk-mini",
                    "eligible_for_active_task": True,
                    "secret": "drop-me",
                },
                {
                    "id": "runner-2",
                    "hostname": "busy-desk",
                    "eligible_for_active_task": False,
                },
            ],
        },
    )

    by_id = _commands_by_id(commands)
    prefer = by_id["session.runner.prefer.runner-1"]
    assert prefer == {
        "id": "session.runner.prefer.runner-1",
        "label": "Prefer desk-mini",
        "group": "runtime",
        "priority": "normal",
        "enabled": True,
        "target": {
            "surface": "runtime",
            "project_id": "project-1",
            "section": "devices",
            "action": "prefer_runner",
            "runner_id": "runner-1",
        },
        "refetch_surfaces": ["strategic_session", "runtime"],
        "executable": True,
        "mutation": {
            "method": "POST",
            "path": "/api/admin/projects/project-1/strategic-session",
            "body": {"workbench": {"preferred_runner_id": "runner-1"}},
        },
    }
    assert "session.runner.prefer.runner-2" not in by_id
    assert "drop-me" not in json.dumps(commands, sort_keys=True)


def test_work_session_commands_include_clear_preferred_runner_mutation():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={
            "interaction_mode": "continuous",
            "selected_refs": [],
            "preferred_runner_id": "runner-1",
        },
        context_package=None,
        actions=[],
        local_runner={
            "runners": [
                {
                    "id": "runner-1",
                    "hostname": "desk-mini",
                    "eligible_for_active_task": True,
                },
            ],
        },
    )

    by_id = _commands_by_id(commands)
    assert by_id["session.runner.prefer.runner-1"]["enabled"] is False
    clear = by_id["session.runner.clear_preferred"]
    assert clear == {
        "id": "session.runner.clear_preferred",
        "label": "Clear preferred runner",
        "group": "runtime",
        "priority": "normal",
        "enabled": True,
        "target": {
            "surface": "runtime",
            "project_id": "project-1",
            "section": "devices",
            "action": "clear_preferred_runner",
            "runner_id": "runner-1",
        },
        "refetch_surfaces": ["strategic_session", "runtime"],
        "executable": True,
        "mutation": {
            "method": "POST",
            "path": "/api/admin/projects/project-1/strategic-session",
            "body": {"workbench": {"preferred_runner_id": None}},
        },
    }


def test_work_session_commands_include_attention_mutations():
    alert_id = str(uuid.uuid4())
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=[],
        attention={
            "counts": {"unread": 2},
            "items": [
                {
                    "id": "alert:alert/one",
                    "kind": "alert",
                    "label": "Runner offline",
                    "detail": "secret-token must not leak",
                    "ref": {
                        "kind": "alert",
                        "id": alert_id,
                        "project_id": "project-1",
                    },
                }
            ],
        },
    )

    by_id = _commands_by_id(commands)
    mark_seen = by_id["attention.messages.mark_seen"]
    assert mark_seen["label"] == "Mark messages seen"
    assert mark_seen["group"] == "attention"
    assert mark_seen["target"] == {
        "surface": "messages",
        "project_id": "project-1",
        "action": "mark_seen",
    }
    assert mark_seen["refetch_surfaces"] == ["messages", "strategic_session"]
    assert mark_seen["mutation"]["method"] == "PUT"
    assert mark_seen["mutation"]["path"] == "/api/admin/messages/last-seen"
    assert mark_seen["mutation"]["body"]["project_id"] == "project-1"
    assert isinstance(mark_seen["mutation"]["body"]["timestamp"], int)

    dismiss = by_id[f"attention.alert.dismiss:{alert_id}"]
    assert dismiss == {
        "id": f"attention.alert.dismiss:{alert_id}",
        "label": f"Dismiss alert: Runner offline ({alert_id[:8]})",
        "group": "attention",
        "priority": "normal",
        "enabled": True,
        "target": {
            "surface": "runtime",
            "project_id": "project-1",
            "section": "health",
            "alert_id": alert_id,
            "action": "dismiss_alert",
        },
        "refetch_surfaces": ["runtime", "strategic_session"],
        "executable": True,
        "mutation": {
            "method": "POST",
            "path": f"/api/admin/alerts/{alert_id}/dismiss",
            "body": {},
        },
    }
    assert "secret-token" not in json.dumps(commands, sort_keys=True)


def test_work_session_command_contract_classifies_emitted_executable_commands():
    alert_id = str(uuid.uuid4())
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={
            "interaction_mode": "monitor",
            "selected_refs": [
                {"kind": "task", "id": "task-1", "label": "Selected task"}
            ],
            "preferred_runner_id": "runner-1",
        },
        context_package=None,
        actions=[],
        local_runner={
            "runners": [
                {
                    "id": "runner-1",
                    "hostname": "desk-mini",
                    "eligible_for_active_task": True,
                },
                {
                    "id": "runner-2",
                    "hostname": "desk-two",
                    "eligible_for_active_task": True,
                },
            ],
        },
        attention={
            "counts": {"unread": 3},
            "items": [
                {
                    "kind": "alert",
                    "label": "Runner offline",
                    "ref": {
                        "kind": "alert",
                        "id": alert_id,
                        "project_id": "project-1",
                    },
                }
            ],
        },
    )

    executable_command_ids = {
        command["id"] for command in commands if "mutation" in command
    }

    assert executable_command_ids == {
        session_mode_command_id("continuous"),
        session_mode_command_id("check_in"),
        session_mode_command_id("monitor"),
        session_mode_command_id("chat"),
        COMMAND_ID_SESSION_CLEAR_SELECTED_REFS,
        runner_preference_command_id("runner-1"),
        runner_preference_command_id("runner-2"),
        COMMAND_ID_SESSION_RUNNER_CLEAR_PREFERRED,
        COMMAND_ID_ATTENTION_MESSAGES_MARK_SEEN,
        alert_dismiss_command_id(alert_id),
    }
    assert {
        work_session_command_kind(command_id)
        for command_id in executable_command_ids
    } == {
        "strategic_workbench",
        "messages_last_seen",
        "alert_dismiss",
    }
    assert all(
        executable_work_session_command_contract(command, project_id="project-1")
        is not None
        for command in commands
        if "mutation" in command
    )


def test_work_session_commands_skip_attention_mutations_when_not_useful():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=[],
        attention={"counts": {"unread": 0}, "items": []},
    )

    ids = {command["id"] for command in commands}
    assert "attention.messages.mark_seen" not in ids
    assert not any(command_id.startswith("attention.alert.dismiss:") for command_id in ids)


def test_work_session_commands_distinguish_duplicate_attention_alert_labels():
    first_alert_id = str(uuid.uuid4())
    second_alert_id = str(uuid.uuid4())
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=[],
        attention={
            "counts": {"unread": 0},
            "items": [
                {
                    "kind": "alert",
                    "label": "Runner offline",
                    "ref": {
                        "kind": "alert",
                        "id": first_alert_id,
                        "project_id": "project-1",
                    },
                },
                {
                    "kind": "alert",
                    "label": "Runner offline",
                    "ref": {
                        "kind": "alert",
                        "id": second_alert_id,
                        "project_id": "project-1",
                    },
                },
            ],
        },
    )

    labels = [
        command["label"]
        for command in commands
        if command["id"].startswith("attention.alert.dismiss:")
    ]
    assert labels == [
        f"Dismiss alert: Runner offline ({first_alert_id[:8]})",
        f"Dismiss alert: Runner offline ({second_alert_id[:8]})",
    ]
    assert labels[0] != labels[1]


def test_work_session_commands_skip_unscoped_attention_alert_mutations():
    valid_alert_id = str(uuid.uuid4())
    wrong_project_alert_id = str(uuid.uuid4())
    malformed_alert_id = str(uuid.uuid4())
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=[],
        attention={
            "counts": {"unread": 0},
            "items": [
                {
                    "kind": "alert",
                    "label": "Missing project",
                    "ref": {"kind": "alert", "id": valid_alert_id},
                },
                {
                    "kind": "alert",
                    "label": "Wrong project",
                    "ref": {
                        "kind": "alert",
                        "id": wrong_project_alert_id,
                        "project_id": "other-project",
                    },
                },
                {
                    "kind": "alert",
                    "label": "Wrong kind",
                    "ref": {
                        "kind": "task",
                        "id": malformed_alert_id,
                        "project_id": "project-1",
                    },
                },
                {
                    "kind": "alert",
                    "label": "Malformed id",
                    "ref": {
                        "kind": "alert",
                        "id": "alert-not-a-uuid",
                        "project_id": "project-1",
                    },
                },
            ],
        },
    )

    assert not any(
        command["id"].startswith("attention.alert.dismiss:")
        for command in commands
    )


def test_work_session_commands_limit_runner_preferences_after_eligibility_filter():
    runners = [
        {
            "id": f"blocked-{index}",
            "hostname": f"blocked-{index}",
            "eligible_for_active_task": False,
        }
        for index in range(9)
    ] + [
        {
            "id": f"runner-{index}",
            "hostname": f"runner-{index}",
            "eligible_for_active_task": True,
        }
        for index in range(10)
    ]
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "continuous", "selected_refs": []},
        context_package=None,
        actions=[],
        local_runner={"runners": runners},
    )

    ids = {command["id"] for command in commands}
    assert "session.runner.prefer.runner-0" in ids
    assert "session.runner.prefer.runner-7" in ids
    assert "session.runner.prefer.runner-8" not in ids
    assert "session.runner.prefer.blocked-0" not in ids


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


def test_work_session_commands_normalize_or_reject_target_sections():
    commands = build_work_session_commands(
        project_id="project-1",
        workbench_state={"interaction_mode": "chat", "selected_refs": []},
        context_package=None,
        actions=[
            {
                "id": "runtime.runners",
                "label": "Open local runners",
                "priority": "normal",
                "source": "runtime",
                "target": {"surface": "runtime", "section": "runners"},
                "enabled": True,
            },
            {
                "id": "runtime.unknown",
                "label": "Open unknown runtime",
                "priority": "normal",
                "source": "runtime",
                "target": {"surface": "runtime", "section": "mystery"},
                "enabled": True,
            },
        ],
    )

    by_id = _commands_by_id(commands)
    assert by_id["runtime.runners"]["target"] == {
        "surface": "runtime",
        "section": "devices",
    }
    assert "runtime.unknown" not in by_id
