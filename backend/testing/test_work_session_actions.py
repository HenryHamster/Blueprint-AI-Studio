from app.work_session_actions import _compact_action_target, build_work_session_actions


def test_work_session_action_target_preserves_canonical_context_fields():
    target = _compact_action_target(
        {
            "surface": " task ",
            "project_id": " project-1 ",
            "section": " artifacts ",
            "task_id": "task-1",
            "message_id": "message-1",
            "direction_id": "direction-1",
            "alert_id": "alert-1",
            "action": "open " + ("x" * 200),
            "document_id": "document-1",
            "artifact_id": "artifact-1",
            "cycle_id": "cycle-1",
            "secret": "drop-me",
        }
    )

    assert target == {
        "surface": "task",
        "project_id": "project-1",
        "section": "artifacts",
        "task_id": "task-1",
        "message_id": "message-1",
        "direction_id": "direction-1",
        "alert_id": "alert-1",
        "action": "open " + ("x" * 152) + "...",
        "document_id": "document-1",
        "artifact_id": "artifact-1",
        "cycle_id": "cycle-1",
    }


def test_work_session_action_task_linked_alert_targets_task():
    actions = build_work_session_actions(
        attention={
            "items": [
                {
                    "id": "alert:alert-1",
                    "kind": "alert",
                    "severity": "needs_operator",
                    "reason": "alert",
                    "label": "Task alert",
                    "detail": "The task emitted a core alert.",
                    "ref": {
                        "kind": "alert",
                        "id": "alert-1",
                        "project_id": "project-1",
                    },
                    "task_id": "task-1",
                    "domain": "core",
                    "event_type": "task_alert",
                }
            ]
        },
        runtime_work=None,
        context_status=None,
    )

    assert actions == [
        {
            "id": "attention.alert:alert-1",
            "kind": "inspect",
            "label": "Inspect alert",
            "reason": "The task emitted a core alert.",
            "priority": "critical",
            "source": "attention",
            "target": {
                "surface": "task",
                "project_id": "project-1",
                "task_id": "task-1",
                "alert_id": "alert-1",
            },
            "enabled": True,
        }
    ]


def test_work_session_action_unlinked_default_alert_targets_runtime_health():
    actions = build_work_session_actions(
        attention={
            "items": [
                {
                    "id": "alert:alert-2",
                    "kind": "alert",
                    "severity": "needs_operator",
                    "reason": "alert",
                    "label": "Core alert",
                    "detail": "A core alert needs inspection.",
                    "ref": {
                        "kind": "alert",
                        "id": "alert-2",
                        "project_id": "project-1",
                    },
                    "domain": "core",
                    "event_type": "generic_alert",
                }
            ]
        },
        runtime_work=None,
        context_status=None,
    )

    assert actions[0]["target"] == {
        "surface": "runtime",
        "project_id": "project-1",
        "alert_id": "alert-2",
        "section": "health",
    }


def test_work_session_actions_rank_runtime_problems_before_low_attention_overflow():
    attention_items = [
        {
            "id": f"message:feedback-{index}",
            "kind": "message",
            "severity": "watch",
            "reason": "feedback",
            "label": f"Feedback {index}",
            "detail": f"Low priority feedback {index}",
            "ref": {
                "kind": "message",
                "id": f"feedback-{index}",
                "project_id": "project-1",
            },
        }
        for index in range(12)
    ]

    actions = build_work_session_actions(
        attention={"items": attention_items},
        runtime_work={
            "compute_jobs": {
                "counts": {
                    "failed_recent": 1,
                    "timed_out_recent": 0,
                }
            }
        },
        context_status=None,
        limit=4,
    )

    assert len(actions) == 4
    assert actions[0]["id"] == "runtime.compute.problem_recent"
    assert actions[0]["priority"] == "high"
    assert [action["id"] for action in actions[1:]] == [
        "attention.message:feedback-0",
        "attention.message:feedback-1",
        "attention.message:feedback-2",
    ]
