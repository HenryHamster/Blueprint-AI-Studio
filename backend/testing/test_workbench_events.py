import uuid
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import (
    Alert,
    AlertSeverity,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
    _utcnow,
)
from app.workbench_events import (
    SURFACE_MESSAGES,
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    WORKBENCH_EVENT_SCHEMA_VERSION,
    _compute_job_feed_items,
    build_workbench_event_feed,
    build_workbench_event_payload,
    emit_workbench_refetch,
    workbench_event_envelope,
)

TEST_USER_ID = "test-user-123"


async def _make_project(session) -> Project:
    project = Project(name="Workbench Events", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


def test_workbench_event_envelope_is_v1_and_dedupes_refetch_surfaces():
    project_id = uuid.uuid4()

    payload = workbench_event_envelope(
        project_id,
        resource="local_runner",
        action="heartbeat",
        refetch_surfaces=(
            SURFACE_STRATEGIC_SESSION,
            "debug",
            SURFACE_RUNTIME,
            SURFACE_STRATEGIC_SESSION,
        ),
    )

    assert payload == {
        "schema_version": WORKBENCH_EVENT_SCHEMA_VERSION,
        "project_id": str(project_id),
        "resource": "local_runner",
        "action": "heartbeat",
        "refetch": {"surfaces": [SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME]},
    }


@pytest.mark.parametrize(
    "payloads,expected",
    [
        ([{"type": "create"}], "create"),
        ([{"type": "  Refactor  "}], "Refactor"),
        ([], None),
        (None, None),
        (["not-a-dict"], None),
        ([{"no_type": "x"}], None),
    ],
)
def test_payload_task_type_reads_first_payload(payloads, expected):
    """_payload_task_type resolves the task type via the mandated
    ``task.first_payload`` accessor (not a hand-rolled ``payloads[0]``)."""
    from app.workbench_events import _payload_task_type

    task = Task(
        project_id=uuid.uuid4(),
        title="task",
        status=TaskStatus.QUEUED,
        payloads=payloads,
    )
    assert _payload_task_type(task) == expected


def test_build_workbench_event_payload_preserves_legacy_top_level_fields():
    project_id = uuid.uuid4()

    payload = build_workbench_event_payload(
        project_id,
        resource="project_message",
        action="created",
        refetch_surfaces=(SURFACE_MESSAGES, SURFACE_STRATEGIC_SESSION),
        summary_key="message",
        summary={"id": "msg-1", "status": "open"},
        message_id="msg-1",
        type="QUESTION",
    )

    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project_id)
    assert payload["resource"] == "project_message"
    assert payload["action"] == "created"
    assert payload["refetch"] == {
        "surfaces": [SURFACE_MESSAGES, SURFACE_STRATEGIC_SESSION],
    }
    assert payload["message_id"] == "msg-1"
    assert payload["type"] == "QUESTION"
    assert payload["message"] == {"id": "msg-1", "status": "open"}


@pytest.mark.asyncio
async def test_emit_workbench_refetch_broadcasts_v1_payload():
    project_id = uuid.uuid4()

    with patch("app.workbench_events.broadcast_to_project", new=AsyncMock()) as bcast:
        await emit_workbench_refetch(
            project_id,
            resource="strategic_check_in",
            action="created",
            refetch_surfaces=(SURFACE_STRATEGIC_SESSION,),
            summary_key="check_in",
            summary={"message_id": "msg-1"},
            message_id="msg-1",
        )

    bcast.assert_awaited_once()
    assert bcast.await_args.args[:2] == (str(project_id), "workbench_refetch")
    payload = bcast.await_args.args[2]
    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project_id)
    assert payload["resource"] == "strategic_check_in"
    assert payload["action"] == "created"
    assert payload["refetch"] == {"surfaces": [SURFACE_STRATEGIC_SESSION]}
    assert payload["message_id"] == "msg-1"
    assert payload["check_in"] == {"message_id": "msg-1"}


@pytest.mark.asyncio
async def test_build_workbench_event_feed_projects_current_durable_sources(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="Pick the runner policy.",
        status="open",
        created_at=now - timedelta(minutes=4),
    )
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        title="Implement runner refresh",
        latest_status_message="Typecheck failed.",
        created_at=now - timedelta(minutes=8),
        updated_at=now - timedelta(minutes=2),
        completed_at=now - timedelta(minutes=2),
    )
    alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="Runner stale",
        message="The local runner stopped heartbeating.",
        domain="runner",
        event_type="local_runner_stale",
        created_at=now - timedelta(minutes=3),
    )
    cycle = StrategicCycle(
        id="cycle-feed-1",
        project_id=project.id,
        outcome="completed",
        summary_text="Strategic pass complete.",
        phase1_started_at=now - timedelta(minutes=15),
        phase1_completed_at=now - timedelta(minutes=10),
        phase2_started_at=now - timedelta(minutes=9),
        phase2_completed_at=now - timedelta(minutes=1),
        tasks_spawned=2,
        directions_created=1,
    )
    db_session.add(message)
    db_session.add(task)
    db_session.add(alert)
    db_session.add(cycle)
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id, limit=3)

    assert feed["version"] == 1
    assert feed["limit"] == 3
    assert feed["has_more"] is True
    assert len(feed["items"]) == 3
    assert [item["resource"] for item in feed["items"]] == [
        "strategic_cycle",
        "task",
        "alert",
    ]
    for item in feed["items"]:
        assert item["schema_version"] == 1
        assert item["project_id"] == str(project.id)
        assert item["occurred_at"]
        assert item["refetch"]["surfaces"]
        assert item["target"]["surface"] in {"task", "runtime", "project", "messages"}

    task_item = next(item for item in feed["items"] if item["resource"] == "task")
    assert task_item["action"] == "failed"
    assert task_item["tone"] == "critical"
    assert task_item["data"]["task_type"] == "create"
    assert task_item["target"] == {"surface": "task", "task_id": str(task.id)}


@pytest.mark.asyncio
async def test_build_workbench_event_feed_is_project_scoped(db_session):
    project = await _make_project(db_session)
    other = await _make_project(db_session)
    db_session.add(
        ProjectMessage(
            project_id=other.id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content="Other project blocker.",
        )
    )
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id)

    assert feed["items"] == []


@pytest.mark.asyncio
async def test_build_workbench_event_feed_reports_more_from_single_source(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    db_session.add_all(
        [
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="system",
                content=f"Message {index}",
                created_at=now - timedelta(minutes=index),
            )
            for index in range(3)
        ]
    )
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id, limit=2)

    assert feed["has_more"] is True
    assert len(feed["items"]) == 2
    assert [item["resource"] for item in feed["items"]] == [
        "project_message",
        "project_message",
    ]


@pytest.mark.asyncio
async def test_build_workbench_event_feed_projects_operator_command_receipts(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    receipt = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Command completed: Monitor mode",
        status="resolved",
        resolved_at=now,
        created_at=now,
        context={
            "event_type": "operator_command",
            "operator_command": {
                "version": 1,
                "command_id": "session.mode.monitor",
                "label": "Monitor mode",
                "status": "succeeded",
                "target": {"surface": "messages", "action": "set_monitor_mode"},
                "refetch_surfaces": ["strategic_session", "runtime"],
                "result": {"kind": "workbench_update"},
            },
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id, limit=1)

    assert feed["items"][0]["resource"] == "operator_command"
    assert feed["items"][0]["action"] == "succeeded"
    assert feed["items"][0]["title"] == "Command: Monitor mode"
    assert feed["items"][0]["target"] == {
        "surface": "messages",
        "action": "set_monitor_mode",
    }
    assert feed["items"][0]["refetch"]["surfaces"] == [
        SURFACE_STRATEGIC_SESSION,
        SURFACE_RUNTIME,
    ]
    assert feed["items"][0]["data"]["command_id"] == "session.mode.monitor"


@pytest.mark.asyncio
async def test_build_workbench_event_feed_normalizes_operator_command_receipt_contract(
    db_session,
):
    project = await _make_project(db_session)
    now = _utcnow()
    receipt = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Command completed: Unknown debug action",
        status="resolved",
        resolved_at=now,
        created_at=now,
        context={
            "event_type": "operator_command",
            "operator_command": {
                "version": 1,
                "command_id": "debug.command",
                "label": "Unknown debug action",
                "status": "succeeded",
                "target": {
                    "surface": "debug",
                    "section": "raw",
                    "secret": "must-not-leak",
                },
                "refetch_surfaces": ["debug"],
                "result": {"kind": "noop"},
            },
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id, limit=1)

    item = feed["items"][0]
    assert item["resource"] == "operator_command"
    assert "target" not in item
    assert item["refetch"]["surfaces"] == [SURFACE_STRATEGIC_SESSION]
    assert "must-not-leak" not in str(item)


@pytest.mark.asyncio
async def test_build_workbench_event_feed_projects_failed_operator_command_receipts(
    db_session,
):
    project = await _make_project(db_session)
    now = _utcnow()
    long_error = "Upstream command handler failed " + ("x" * 500)
    receipt = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Command failed: Dismiss alert - " + long_error,
        status="resolved",
        resolved_at=now,
        created_at=now,
        context={
            "event_type": "operator_command",
            "operator_command": {
                "version": 1,
                "command_id": "attention.alert.dismiss:"
                "11111111-1111-1111-1111-111111111111",
                "label": "Dismiss alert",
                "status": "failed",
                "target": {
                    "surface": "runtime",
                    "section": "health",
                    "alert_id": "11111111-1111-1111-1111-111111111111",
                },
                "refetch_surfaces": ["runtime", "strategic_session"],
                "result": {
                    "kind": "command_failed",
                    "status_code": 503,
                    "error": long_error,
                },
            },
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id, limit=1)

    item = feed["items"][0]
    assert item["resource"] == "operator_command"
    assert item["action"] == "failed"
    assert item["tone"] == "critical"
    assert item["summary"].startswith("Command failed: Dismiss alert")
    assert len(item["summary"]) <= 280
    assert item["target"] == {
        "surface": "runtime",
        "section": "health",
        "alert_id": "11111111-1111-1111-1111-111111111111",
    }
    assert item["refetch"]["surfaces"] == [
        SURFACE_RUNTIME,
        SURFACE_STRATEGIC_SESSION,
    ]
    assert item["data"]["result"]["status_code"] == 503
    assert item["data"]["result"]["error"].endswith("...")
    assert len(item["data"]["result"]["error"]) <= 240


@pytest.mark.asyncio
async def test_build_workbench_event_feed_orders_tasks_by_semantic_activity(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    stale_heartbeat_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        title="Long running task",
        latest_status_message="Still running heartbeat update.",
        status_log=[
            {
                "t": now.isoformat(),
                "msg": "Still running heartbeat update.",
            }
        ],
        created_at=now - timedelta(hours=2),
        started_at=now - timedelta(hours=1),
        updated_at=now,
    )
    message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Recent operator-relevant update.",
        created_at=now - timedelta(minutes=5),
    )
    db_session.add(stale_heartbeat_task)
    db_session.add(message)
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id, limit=2)

    assert [item["resource"] for item in feed["items"]] == [
        "project_message",
        "task",
    ]
    task_item = next(item for item in feed["items"] if item["resource"] == "task")
    assert task_item["occurred_at"] == stale_heartbeat_task.started_at.replace(
        tzinfo=timezone.utc
    ).isoformat()


@pytest.mark.asyncio
async def test_build_workbench_event_feed_uses_task_status_transition_time(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        title="Recently claimed task",
        status_log=[
            {
                "t": (now - timedelta(minutes=2)).isoformat(),
                "msg": "Status changed: PROVISIONING -> IN_PROGRESS",
            }
        ],
        created_at=now - timedelta(hours=2),
        started_at=now - timedelta(hours=1),
        updated_at=now - timedelta(minutes=2),
    )
    message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Older note.",
        created_at=now - timedelta(minutes=5),
    )
    db_session.add(task)
    db_session.add(message)
    await db_session.commit()

    feed = await build_workbench_event_feed(db_session, project.id, limit=2)

    assert [item["resource"] for item in feed["items"]] == [
        "task",
        "project_message",
    ]
    assert feed["items"][0]["occurred_at"] == (
        now - timedelta(minutes=2)
    ).replace(tzinfo=timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_build_workbench_event_feed_adds_runtime_runner_and_compute_items(
    db_session,
):
    project = await _make_project(db_session)
    now = _utcnow()
    runner_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    runtime_summary = {
        "local_runner": {
            "mode": "local",
            "status": "working",
            "location": "locally",
            "status_reason": None,
            "strategic_runner_count": 1,
            "live_runner_count": 1,
            "eligible_live_count": 1,
            "active_runner_id": runner_id,
            "continuation": {"preferred_runner_id": runner_id},
            "runners": [
                {
                    "id": runner_id,
                    "hostname": "workstation-01",
                    "last_seen_at": now.isoformat(),
                    "created_at": (now - timedelta(hours=1)).isoformat(),
                    "capabilities": {
                        "runner_preset": "strategic_local",
                        "has_gpu": True,
                        "max_concurrent": 2,
                        "secret_token": "must-not-leak",
                    },
                    "health": {
                        "device_status": "ready",
                        "is_live": True,
                        "agent_available": True,
                        "active_compute_job_count": 1,
                        "available_slots": 1,
                        "raw_polling_metadata": {"token": "must-not-leak"},
                    },
                    "eligible_for_active_task": True,
                    "is_active_runner": True,
                    "is_preferred": True,
                }
            ],
        },
        "work": {
            "compute_jobs": {
                "items": [
                    {
                        "id": job_id,
                        "task_id": str(uuid.uuid4()),
                        "task_title": "Run GPU benchmark",
                        "task_type": "compute_experiment",
                        "status": "RUNNING",
                        "backend": "runner_subprocess",
                        "machine_type": "gpu-small",
                        "accelerator_type": "L4",
                        "accelerator_count": 1,
                        "spend_cap_usd": 8.0,
                        "cost_usd": 1.25,
                        "created_at": (now - timedelta(minutes=30)).isoformat(),
                        "updated_at": (now - timedelta(minutes=1)).isoformat(),
                        "started_at": (now - timedelta(minutes=20)).isoformat(),
                        "last_heartbeat_at": (
                            now - timedelta(seconds=30)
                        ).isoformat(),
                        "next_poll_at": now.isoformat(),
                        "polling_metadata": {"token": "must-not-leak"},
                    }
                ]
            }
        },
    }

    feed = await build_workbench_event_feed(
        db_session,
        project.id,
        limit=5,
        runtime_summary=runtime_summary,
    )

    by_resource = {item["resource"]: item for item in feed["items"]}
    assert set(by_resource) == {"local_runner", "compute_job"}

    runner_item = by_resource["local_runner"]
    assert runner_item["action"] == "selected"
    assert runner_item["target"] == {
        "surface": "runtime",
        "section": "devices",
        "runner_id": runner_id,
    }
    assert runner_item["data"]["health"]["active_compute_job_count"] == 1
    assert runner_item["data"]["capabilities"] == {
        "runner_preset": "strategic_local",
        "has_gpu": True,
        "max_concurrent": 2,
    }

    compute_item = by_resource["compute_job"]
    assert compute_item["action"] == "running"
    assert compute_item["title"] == "Compute job running: Run GPU benchmark"
    assert compute_item["target"]["section"] == "compute"
    assert compute_item["data"]["backend"] == "runner_subprocess"
    assert compute_item["data"]["accelerator_type"] == "L4"
    assert "next_poll_at" not in compute_item["data"]
    assert "polling_metadata" not in compute_item["data"]
    assert "must-not-leak" not in str(feed["items"])


@pytest.mark.asyncio
async def test_build_workbench_event_feed_tolerates_malformed_runtime_counts(
    db_session,
):
    project = await _make_project(db_session)
    runtime_summary = {
        "local_runner": {
            "mode": "pending_local",
            "status": "waiting",
            "active_task_pinned": True,
            "strategic_runner_count": "not-a-number",
            "live_runner_count": object(),
            "eligible_live_count": None,
            "continuation": {"preferred_runner_id": "runner-pref"},
            "status_reason": "No runner heartbeat yet.",
            "runners": [],
        }
    }

    feed = await build_workbench_event_feed(
        db_session,
        project.id,
        runtime_summary=runtime_summary,
    )

    assert len(feed["items"]) == 1
    item = feed["items"][0]
    assert item["resource"] == "local_runner"
    assert item["action"] == "pending_local"
    assert item["data"]["strategic_runner_count"] == 0
    assert item["data"]["live_runner_count"] == 0
    assert item["data"]["eligible_live_count"] == 0


def test_compute_job_feed_item_criterion_met_uses_active_tone():
    project_id = uuid.uuid4()
    job_id = str(uuid.uuid4())
    items = _compute_job_feed_items(
        project_id,
        {
            "compute_jobs": {
                "items": [
                    {
                        "id": job_id,
                        "task_id": str(uuid.uuid4()),
                        "task_title": "GPU throughput benchmark",
                        "task_type": "compute_experiment",
                        "status": "SUCCEEDED",
                        "experiment_outcome": {
                            "outcome_label": "met",
                            "criterion_met": True,
                            "criterion_summary": "Throughput exceeded 100 req/s",
                            "hypothesis_held": True,
                        },
                    }
                ]
            }
        },
    )
    assert len(items) == 1
    item = items[0]
    assert item["tone"] == "active"
    assert "criterion met" in item["title"].lower()
    assert "GPU throughput benchmark" in item["title"]
    assert item["summary"] == "Throughput exceeded 100 req/s"
    assert item["data"]["experiment_outcome"]["outcome_label"] == "met"
    assert item["data"]["experiment_outcome"]["criterion_met"] is True
    assert item["data"]["experiment_outcome"]["hypothesis_held"] is True


def test_compute_job_feed_item_unmet_criterion_uses_warning_tone():
    project_id = uuid.uuid4()
    job_id = str(uuid.uuid4())
    items = _compute_job_feed_items(
        project_id,
        {
            "compute_jobs": {
                "items": [
                    {
                        "id": job_id,
                        "task_id": str(uuid.uuid4()),
                        "task_title": "Latency regression check",
                        "task_type": "compute_experiment",
                        "status": "SUCCEEDED",
                        "experiment_outcome": {
                            "outcome_label": "unmet",
                            "criterion_met": False,
                            "criterion_summary": "P99 latency was 380ms, threshold was 200ms",
                            "hypothesis_held": None,
                        },
                    }
                ]
            }
        },
    )
    assert len(items) == 1
    item = items[0]
    assert item["tone"] == "warning"
    assert "criterion unmet" in item["title"].lower()
    assert "Latency regression check" in item["title"]
    assert item["summary"] == "P99 latency was 380ms, threshold was 200ms"
    assert item["data"]["experiment_outcome"]["outcome_label"] == "unmet"
    assert item["data"]["experiment_outcome"]["criterion_met"] is False
