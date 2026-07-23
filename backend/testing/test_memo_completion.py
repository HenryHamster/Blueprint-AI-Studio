"""Tests for the memo task completion handler.

Memo is a terminal reasoning-artifact task type: parses the agent's
structured JSON output, records the parsed object into task.plan_output['memo'],
and does NOT spawn any child tasks or open a PR. No separate MEMO ``Artifact``
row is written — the memo text lives solely in plan_output, which is what every
memo consumer reads (see query_recent_memo_citations).
"""

import json
from datetime import timezone
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth import create_agent_token
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.task_completion import (
    _apply_synthesized_memo,
    _handle_memo_completion,
    _normalize_memo,
    _parse_memo_output,
    _try_recover_memo_from_parse_failure,
)
from testing.factories import make_project, make_task


# ── _parse_memo_output ──────────────────────────────────────────────────────


class TestParseMemoOutput:
    def test_bare_json_object(self):
        text = json.dumps(
            {
                "summary": "s",
                "reasoning": "r",
                "citations": [],
                "tags": [],
            }
        )
        out = _parse_memo_output(text)
        assert out is not None
        assert out["summary"] == "s"

    def test_object_with_surrounding_text(self):
        body = (
            "Thinking...\nHere is the memo:\n"
            + json.dumps(
                {
                    "summary": "ok",
                    "reasoning": "details",
                    "citations": ["task:abc"],
                    "tags": ["t"],
                }
            )
            + "\nend"
        )
        out = _parse_memo_output(body)
        assert out is not None
        assert out["citations"] == ["task:abc"]

    def test_empty_returns_none(self):
        assert _parse_memo_output("") is None
        assert _parse_memo_output(None) is None  # type: ignore[arg-type]

    def test_missing_keys_returns_none(self):
        # Has '{' but no summary/reasoning keys
        assert _parse_memo_output('{"hello": "world"}') is None


# ── _normalize_memo ─────────────────────────────────────────────────────────


class TestNormalizeMemo:
    def test_coerces_non_string_to_string(self):
        norm = _normalize_memo(
            {"summary": 1, "reasoning": True, "citations": None, "tags": None}
        )
        assert norm["summary"] == "1"
        assert norm["reasoning"] == "True"
        assert norm["citations"] == []
        assert norm["tags"] == []

    def test_truncates_long_lists(self):
        norm = _normalize_memo(
            {
                "summary": "s",
                "reasoning": "r",
                "citations": [f"c{i}" for i in range(100)],
                "tags": [f"t{i}" for i in range(100)],
            }
        )
        assert len(norm["citations"]) == 50
        assert len(norm["tags"]) == 20


# ── _handle_memo_completion (integration with db_session) ───────────────────


@pytest.fixture
def _quiet_broadcast(mocker):
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )


@pytest.mark.asyncio
async def test_memo_completion_records_plan_output_and_no_children(
    db_session, _quiet_broadcast
):
    """A well-formed memo: records the memo into plan_output, writes NO Artifact
    row, spawns ZERO child tasks, and does not produce a PR artifact."""
    project = make_project(name="MemoProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: explain X",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "memo", "topic": "X", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    memo = {
        "summary": "X is interesting because Y",
        "reasoning": "Detailed paragraphs explaining the connection.",
        "citations": ["task:" + str(task.id), "direction:d-1234"],
        "tags": ["theory", "review"],
    }
    logs = json.dumps(memo)

    await _handle_memo_completion(task, logs, db_session)

    # 1) No Artifact row is written — the memo lives only in plan_output. The
    #    MEMO Artifact was a write-only shadow copy no reader queried by type.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []

    # 2) task.plan_output carries the full parsed memo — the canonical store.
    await db_session.refresh(task)
    assert isinstance(task.plan_output, dict)
    assert task.plan_output["memo"]["summary"] == memo["summary"]
    assert task.plan_output["memo"]["reasoning"] == memo["reasoning"]
    assert task.plan_output["memo"]["citations"] == memo["citations"]
    assert task.plan_output["memo"]["tags"] == memo["tags"]

    # 3) No child tasks spawned
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert children == []

    # 5) Task is marked COMPLETED directly. Memo tasks spawn no children and
    #    open no PR, so no lifecycle path would promote a childless, PR-less
    #    EVALUATING task — and query_recent_memo_citations only reads COMPLETED
    #    memos, so COMPLETED is the reader/writer contract.
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_memo_completion_unparseable_fails_forward_quality_gate(
    db_session, _quiet_broadcast
):
    """Forward-quality gate: if the agent's output can't be parsed as a memo
    object, the task must NOT complete clean. It creates no artifact, spawns no
    children, and is marked FAILED (not COMPLETED) with the gate verdict
    recorded — a memo opens no PR and spawns no children, so EVALUATING would
    hang forever; FAILED is the honest terminal outcome for bad output."""
    project = make_project(name="MemoBad")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: garbage",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "memo", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_memo_completion(task, "this is not json at all", db_session)

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert children == []

    await db_session.refresh(task)
    # Did NOT complete clean — the gate failed it.
    assert task.status == TaskStatus.FAILED
    assert task.latest_status_message is not None
    assert "gate" in task.latest_status_message.lower()
    # Gate verdict is recorded on the task for observability.
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "forward-quality"


@pytest.mark.asyncio
async def test_memo_completion_empty_output_fails_gate(db_session, _quiet_broadcast):
    """An empty terminal artifact fails the forward-quality gate and does not
    complete clean."""
    project = make_project(name="MemoEmpty")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: empty",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "memo", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_memo_completion(task, "   ", db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []


@pytest.mark.asyncio
async def test_memo_payload_schema_accepts_optional_fields():
    """MemoPayload validates with no required fields beyond base; preserves
    the optional fields that callers stamp."""
    from app.payload_schemas import validate_task_payload

    raw = {
        "type": "memo",
        "topic": "session memory",
        "reasoning_kind": "deferred_finding",
        "source_task_id": "task-123",
        "source_finding": {"id": "f-1", "title": "X"},
        "direction_id": "d-abc",
        "prompt": "Explain why we dropped this finding.",
        "base_branch": "main",
    }
    out = validate_task_payload(raw)
    assert out["type"] == "memo"
    assert out["topic"] == "session memory"
    assert out["source_finding"] == {"id": "f-1", "title": "X"}


def test_memo_capability_registered_and_read_only():
    """The memo TaskCapability is registered with agent_mode='read_only'."""
    from app.task_capabilities import (
        READ_ONLY_TASK_TYPES,
        TaskType,
        get_task_capability,
    )

    cap = get_task_capability(TaskType.memo)
    assert cap is not None
    assert cap.agent_mode == "read_only"
    assert cap.label == "Memo"
    assert "memo" in READ_ONLY_TASK_TYPES


def test_artifact_type_memo_exists():
    """ArtifactType.MEMO is retained for read-model compatibility.

    New memo completions no longer write MEMO Artifact rows, but the enum value
    must remain so historical MEMO rows (a plain String column) still load and
    coerce back to ArtifactType.MEMO without a validation error.
    """
    assert ArtifactType.MEMO.value == "MEMO"


# ── Endpoint-path regression: no Artifact row, memo recorded to plan_output ──
#
# The agent's memo success post deliberately OMITS artifact_type (see
# agent/src/handlers/memo.ts). The status endpoint's generic Artifact-creation
# block is gated on `if payload.artifact_type:`, so omitting it means the memo
# path (dispatched on task_type=="memo") writes NO Artifact row at all — the
# memo is recorded into plan_output by _handle_memo_completion. This test drives
# the real /api/agent/status endpoint to lock in the no-artifact invariant — a
# regression that re-introduced artifact_type='MEMO' on the agent side would
# create a stray Artifact row and fail here.
@pytest.mark.asyncio
async def test_memo_status_endpoint_writes_no_artifact(
    client: AsyncClient, db_session, mocker
):
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )

    project = Project(name="MemoEndpoint", user_id="test-user-memo")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "topic": "X", "base_branch": "main"}],
        vm_instance_id="agent-vm-memo-endpoint",
        status=TaskStatus.IN_PROGRESS,
    )
    # Sibling stays IN_PROGRESS to avoid the full-project aggregation commit
    # path, which has a SQLite-async lazy-reload limitation in tests.
    sibling = Task(
        project_id=project.id,
        payloads=[{"cmd": "other"}],
        vm_instance_id="agent-vm-memo-sibling",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    db_session.add(sibling)
    await db_session.commit()
    await db_session.refresh(task)

    memo = {
        "summary": "X is interesting because Y",
        "reasoning": "Detailed paragraphs explaining the connection.",
        "citations": ["task:" + str(task.id)],
        "tags": ["theory"],
    }

    token = create_agent_token(str(task.id), "agent-vm-memo-endpoint")
    headers = {"x-agent-token": token}
    # NOTE: no artifact_type — mirrors the agent's memo success post.
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "logs": json.dumps(memo),
    }

    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    # No Artifact row is created by the memo path.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []

    # The memo is recorded into plan_output — the canonical store consumers read.
    await db_session.refresh(task)
    assert isinstance(task.plan_output, dict)
    assert task.plan_output["memo"]["summary"] == memo["summary"]


# ── _apply_synthesized_memo (spawn-time completion) ─────────────────────────


@pytest.mark.asyncio
async def test_apply_synthesized_memo_creates_completed_task_no_artifact(
    db_session, _quiet_broadcast
):
    """Dispatcher memos whose reasoning is known at spawn time are recorded to
    plan_output and marked COMPLETED immediately — never entering QUEUED, and
    without writing an Artifact row."""
    project = make_project(name="SynthMemoProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: synthesized",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "memo", "topic": "X", "base_branch": "main"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    memo = {
        "summary": "Synthesized at spawn",
        "reasoning": "Full reasoning known at spawn time.",
        "citations": [f"task:{task.id}"],
        "tags": ["regression-false-positive"],
    }

    _apply_synthesized_memo(task, memo, db_session)
    await db_session.commit()
    await db_session.refresh(task)

    # Task is COMPLETED immediately — never lingered in QUEUED
    assert task.status == TaskStatus.COMPLETED

    # No Artifact row is written.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []

    # plan_output carries the full memo — the canonical store.
    assert isinstance(task.plan_output, dict)
    assert task.plan_output["memo"]["summary"] == "Synthesized at spawn"
    assert task.plan_output["memo"]["citations"] == [f"task:{task.id}"]
    assert task.plan_output["memo"]["tags"] == ["regression-false-positive"]


@pytest.mark.asyncio
async def test_inline_memo_carries_no_vm_attribution(db_session, _quiet_broadcast):
    """_apply_synthesized_memo clears vm_instance_id and vm_provisioned_at so
    orchestrator-synthesized memos never show a phantom agent-VM in the UI.
    Covers deferred_finding, direction_retirement, and outcome_scaffolding kinds.
    """
    from datetime import datetime, timezone

    project = make_project(name="NoVmAttrProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    memo_content = {
        "summary": "Test memo",
        "reasoning": "Inline synthesis — no VM required.",
        "citations": [],
        "tags": ["vm-free"],
    }

    for reasoning_kind in ("deferred_finding", "direction_retirement", "outcome_scaffolding"):
        task = make_task(
            project_id=project.id,
            title=f"Memo: {reasoning_kind}",
            status=TaskStatus.QUEUED,
            payloads=[{"type": "memo", "reasoning_kind": reasoning_kind, "base_branch": "main"}],
            vm_instance_id=f"agent-vm-placeholder-{reasoning_kind}",
            vm_provisioned_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        _apply_synthesized_memo(task, memo_content, db_session)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.status == TaskStatus.COMPLETED, f"{reasoning_kind}: expected COMPLETED"
        assert task.vm_instance_id is None, (
            f"{reasoning_kind}: vm_instance_id should be cleared; got {task.vm_instance_id!r}"
        )
        assert task.vm_provisioned_at is None, (
            f"{reasoning_kind}: vm_provisioned_at should be None; got {task.vm_provisioned_at!r}"
        )


@pytest.mark.asyncio
async def test_scaffolding_memo_synthesis_failure_leaves_no_queued_task(
    db_session, _quiet_broadcast, mocker
):
    """When _apply_synthesized_memo raises inside the outcome-scaffolding try/except,
    the memo task must be marked FAILED (not QUEUED) so the dispatcher never provisions
    a VM for it.
    """
    from datetime import datetime

    from app.direction_lifecycle import _process_direction_lifecycle
    from app.models import Project as ProjectModel

    # Patch _check_outcome_scaffolding to trigger the first-detection scaffolding path.
    _fake_evidence = {
        "total_cycles": 12,
        "stall_run_cycles": 6,
        "tasks_completed": 20,
        "tasks_failed": 0,
        "lifetime_merged": 10,
        "lifetime_merge_rate": 0.80,
        "last_cycle_at": "2024-01-01T00:00:00Z",
    }
    mocker.patch(
        "app.strategic_completion._check_outcome_scaffolding",
        return_value=_fake_evidence,
    )
    # Patch _apply_synthesized_memo to simulate a synthesis failure.
    mocker.patch(
        "app.task_completion._apply_synthesized_memo",
        side_effect=RuntimeError("simulated synthesis failure"),
    )
    # Silence unrelated async calls in the lifecycle function.
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
        return_value=None,
    )

    project = ProjectModel(
        name="ScaffoldFailProj",
        user_id="u-scaffold-fail",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-scaffold-fail",
                "text": "Test direction for scaffolding failure",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2024-01-01T00:00:00",
                # outcome_scaffolding_flagged absent = first detection triggers memo creation
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    import app.shared_state as _ss
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    await db_session.commit()

    memo_tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    queued_memos = [t for t in memo_tasks if t.status == TaskStatus.QUEUED]
    assert queued_memos == [], (
        f"Expected no QUEUED memo tasks after synthesis failure; got {[t.title for t in queued_memos]}"
    )
    failed_memos = [t for t in memo_tasks if t.status == TaskStatus.FAILED]
    assert len(failed_memos) >= 1, (
        "Expected at least one FAILED memo task after synthesis failure"
    )


# ── _try_recover_memo_from_parse_failure ────────────────────────────────────


def _make_codex_jsonl(memo: dict) -> str:
    """Build the Codex JSONL output that contains a memo JSON inside an
    item.completed agent_message envelope — the exact shape the failing task had."""
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "tid-001"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(memo),
                    },
                }
            ),
            json.dumps({"type": "turn.ended"}),
        ]
    )


@pytest.mark.asyncio
async def test_recover_memo_from_parse_failure_success(db_session, _quiet_broadcast):
    """Recovery succeeds when the JSONL error log contains a valid memo JSON
    inside an item.completed agent_message envelope; task is promoted to
    COMPLETED with the memo recorded to plan_output (no Artifact row)."""
    project = make_project(name="RecoveryProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: recovery test",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "topic": "Y", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={"agent_failure_category_raw": "parse_failed"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    memo = {
        "summary": "Recovered summary",
        "reasoning": "Recovered reasoning explaining the finding.",
        "citations": ["task:abc123", "direction:d-9999"],
        "tags": ["recovery", "test"],
    }
    logs = _make_codex_jsonl(memo)

    recovered = await _try_recover_memo_from_parse_failure(task, logs, db_session)

    assert recovered is True

    # Task promoted to COMPLETED
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    # No Artifact row written — the recovered memo lives in plan_output.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []

    # plan_output carries the recovered memo.
    assert task.plan_output["memo"]["summary"] == memo["summary"]
    assert task.plan_output["memo"]["citations"] == memo["citations"]
    assert task.plan_output["memo"]["tags"] == memo["tags"]


@pytest.mark.asyncio
async def test_recover_memo_from_parse_failure_no_envelope(
    db_session, _quiet_broadcast
):
    """When the JSONL log contains no item.completed agent_message envelope,
    recovery returns False and leaves the task FAILED without creating an artifact."""
    project = make_project(name="RecoveryNoEnvProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: no envelope",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "tid-002"}),
            "some plain text that is not JSON",
            json.dumps({"type": "turn.ended"}),
        ]
    )

    recovered = await _try_recover_memo_from_parse_failure(task, logs, db_session)

    assert recovered is False
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []


@pytest.mark.asyncio
async def test_recover_memo_from_parse_failure_missing_keys(
    db_session, _quiet_broadcast
):
    """When the agent_message text contains JSON but is missing required memo keys,
    recovery returns False and the task remains FAILED."""
    project = make_project(name="RecoveryMissingKeys")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: missing keys",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    incomplete_memo = {"summary": "only summary", "reasoning": "only reasoning"}
    logs = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(incomplete_memo),
                    },
                }
            ),
        ]
    )

    recovered = await _try_recover_memo_from_parse_failure(task, logs, db_session)

    assert recovered is False
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_recover_memo_from_parse_failure_empty_logs(db_session, _quiet_broadcast):
    """Empty or None logs return False immediately without touching the task."""
    project = make_project(name="RecoveryEmpty")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: empty logs",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert await _try_recover_memo_from_parse_failure(task, "", db_session) is False
    assert await _try_recover_memo_from_parse_failure(task, None, db_session) is False  # type: ignore[arg-type]
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_recover_memo_from_incomplete_failure(db_session, _quiet_broadcast):
    """Recovery is also attempted when raw_fc == 'incomplete' — a stale handler that
    exits cleanly (exit_code=0) without emitting turn.completed still produces a valid
    item.completed envelope that _try_recover_memo_from_parse_failure can extract."""
    project = make_project(name="RecoveryIncompleteProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: incomplete recovery test",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "topic": "Z", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={"agent_failure_category_raw": "incomplete"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    memo = {
        "summary": "Incomplete recovery summary",
        "reasoning": "Reasoning from an incomplete but otherwise valid memo agent run.",
        "citations": ["task:xyz789"],
        "tags": ["incomplete", "recovery"],
    }
    logs = _make_codex_jsonl(memo)

    recovered = await _try_recover_memo_from_parse_failure(task, logs, db_session)

    assert recovered is True

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []

    assert task.plan_output["memo"]["summary"] == memo["summary"]
    assert task.plan_output["memo"]["citations"] == memo["citations"]


# ── Strategy 1: recovered-agent-message-text marker blocks ──────────────────


@pytest.mark.asyncio
async def test_recover_memo_from_recovered_marker_block(db_session, _quiet_broadcast):
    """Recovery succeeds when logs contain a '--- recovered-agent-message-text ---'
    block with a valid memo JSON — the strategy-1 path added to support the new
    agent failure-log format."""
    project = make_project(name="RecoveryMarkerProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: marker recovery",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "topic": "Z", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={"agent_failure_category_raw": "parse_failed"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    memo = {
        "summary": "Marker-recovered summary",
        "reasoning": "Marker-recovered reasoning with full detail.",
        "citations": ["task:marker123", "direction:d-marker"],
        "tags": ["marker-recovery", "test"],
    }
    # Simulate the failure log the agent now emits: error header + tail + marker block
    logs = (
        "Memo handler could not extract a JSON object with all required keys "
        "(summary, reasoning, citations, tags). exitCode=0, allOutput=3000b, "
        "lastResultText=200b\n"
        "--- output tail ---\n"
        "...truncated tail...\n"
        "--- recovered-agent-message-text ---\n" + json.dumps(memo) + "\n"
    )

    recovered = await _try_recover_memo_from_parse_failure(task, logs, db_session)

    assert recovered is True
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []
    assert task.plan_output["memo"]["summary"] == memo["summary"]
    assert task.plan_output["memo"]["citations"] == memo["citations"]
    assert task.plan_output["memo"]["tags"] == memo["tags"]


@pytest.mark.asyncio
async def test_recover_memo_from_recovered_marker_block_multiple_blocks(
    db_session, _quiet_broadcast
):
    """When multiple marker blocks are present (first invalid, second valid),
    recovery succeeds using the first valid block."""
    project = make_project(name="RecoveryMultiMarker")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: multi-marker",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    valid_memo = {
        "summary": "Valid block summary",
        "reasoning": "Valid reasoning here.",
        "citations": [],
        "tags": ["multi"],
    }
    # First block has only partial keys (invalid); second has the full memo
    logs = (
        "--- output tail ---\n...\n"
        "--- recovered-agent-message-text ---\n"
        + json.dumps({"summary": "partial", "reasoning": "only two keys"})
        + "\n"
        "--- recovered-agent-message-text ---\n" + json.dumps(valid_memo) + "\n"
    )

    recovered = await _try_recover_memo_from_parse_failure(task, logs, db_session)

    assert recovered is True
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["memo"]["summary"] == valid_memo["summary"]


# ── Strategy 3: escaped-JSON tail fallback ───────────────────────────────────


@pytest.mark.asyncio
async def test_recover_memo_from_escaped_tail(db_session, _quiet_broadcast):
    """Recovery succeeds when logs contain only the suffix of an agent_message
    envelope (truncated mid-line) and the memo JSON is visible in escaped form
    — the strategy-3 escaped-JSON fallback path."""
    project = make_project(name="RecoveryEscapedTail")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Memo: escaped tail",
        status=TaskStatus.FAILED,
        payloads=[{"type": "memo", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={"agent_failure_category_raw": "parse_failed"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    memo = {
        "summary": "Escaped tail summary",
        "reasoning": "Escaped tail reasoning that was recovered from a truncated log.",
        "citations": ["direction:d-escape"],
        "tags": ["escape-recovery"],
    }

    # Build the JSONL line that would appear in allOutput using compact JSON
    # (no spaces after : or ,) to match the TypeScript JSON.stringify format.
    full_line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(memo, separators=(",", ":")),
            },
        },
        separators=(",", ":"),
    )
    # Simulate the tail being truncated: only part of the JSONL line
    # appears in the logs — starting mid-line at the "text" key.
    tail_start = full_line.find('"text":"')
    assert tail_start != -1, "Compact JSON must contain 'text\":\"' without spaces"
    truncated_tail = full_line[tail_start:]

    logs = (
        "Memo handler could not extract a JSON object ...\n"
        "--- output tail ---\n" + truncated_tail
    )

    recovered = await _try_recover_memo_from_parse_failure(task, logs, db_session)

    assert recovered is True
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []
    assert task.plan_output["memo"]["summary"] == memo["summary"]
    assert task.plan_output["memo"]["citations"] == memo["citations"]
