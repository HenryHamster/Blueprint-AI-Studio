"""Tests for the memo task completion handler.

Memo is a terminal reasoning-artifact task type: parses the agent's
structured JSON output, persists exactly one Artifact row with
artifact_type=MEMO, mirrors the parsed object into task.plan_output['memo'],
and does NOT spawn any child tasks or open a PR.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth import create_agent_token
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.task_completion import (
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
async def test_memo_completion_persists_artifact_and_no_children(
    db_session, _quiet_broadcast
):
    """A well-formed memo: persists ONE MEMO Artifact, mirrors to plan_output,
    spawns ZERO child tasks, and does not produce a PR artifact."""
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

    # 1) Exactly one Artifact row of type MEMO
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art.artifact_type == ArtifactType.MEMO
    assert art.pr_url is None
    persisted = json.loads(art.logs)
    assert persisted["summary"] == memo["summary"]
    assert persisted["citations"] == memo["citations"]
    assert persisted["tags"] == memo["tags"]

    # 2) task.plan_output mirrors the parsed memo
    await db_session.refresh(task)
    assert isinstance(task.plan_output, dict)
    assert task.plan_output["memo"]["summary"] == memo["summary"]
    assert task.plan_output["memo"]["tags"] == memo["tags"]

    # 3) No child tasks spawned
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert children == []

    # 4) No PULL_REQUEST artifact created
    pr_arts = [a for a in artifacts if a.artifact_type == ArtifactType.PULL_REQUEST]
    assert pr_arts == []

    # 5) Task is marked COMPLETED directly. Memo tasks spawn no children and
    #    open no PR, so no lifecycle path would promote a childless, PR-less
    #    EVALUATING task — and query_recent_memo_citations only reads COMPLETED
    #    memos, so COMPLETED is the reader/writer contract.
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_memo_completion_unparseable_does_not_create_artifact(
    db_session, _quiet_broadcast
):
    """If the agent's output can't be parsed as a memo object, we record a
    status message but do NOT create an artifact and do NOT spawn children."""
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
    assert task.latest_status_message is not None
    assert "no parseable" in task.latest_status_message.lower()
    # Terminal even when uncitable — do not leave it lingering in EVALUATING.
    assert task.status == TaskStatus.COMPLETED


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
    """ArtifactType.MEMO must exist for the completion handler to use it."""
    assert ArtifactType.MEMO.value == "MEMO"


# ── Endpoint-path regression: single MEMO artifact, no double creation ───────
#
# The agent's memo success post deliberately OMITS artifact_type (see
# agent/src/handlers/memo.ts). The status endpoint's generic Artifact-creation
# block is gated on `if payload.artifact_type:`, so omitting it means the ONLY
# writer of the MEMO Artifact is _handle_memo_completion (dispatched on
# task_type=="memo", independent of artifact_type). This test drives the real
# /api/agent/status endpoint to lock in that exactly-one invariant — the
# earlier unit tests call _handle_memo_completion directly and would miss a
# regression that re-introduced artifact_type='MEMO' on the agent side.
@pytest.mark.asyncio
async def test_memo_status_endpoint_creates_exactly_one_artifact(
    client: AsyncClient, db_session, mocker
):
    mocker.patch("app.main.delete_agent_vm", return_value=None)
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

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == ArtifactType.MEMO
    persisted = json.loads(artifacts[0].logs)
    assert persisted["summary"] == memo["summary"]


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
    COMPLETED with a MEMO Artifact."""
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

    # Exactly one MEMO Artifact written
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == ArtifactType.MEMO
    persisted = json.loads(artifacts[0].logs)
    assert persisted["summary"] == memo["summary"]
    assert persisted["citations"] == memo["citations"]
    assert persisted["tags"] == memo["tags"]

    # plan_output mirrors the memo
    assert task.plan_output["memo"]["summary"] == memo["summary"]


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
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == ArtifactType.MEMO
    persisted = json.loads(artifacts[0].logs)
    assert persisted["summary"] == memo["summary"]
    assert persisted["citations"] == memo["citations"]

    assert task.plan_output["memo"]["summary"] == memo["summary"]
