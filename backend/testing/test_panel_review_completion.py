"""Tests for the panel_review task type and structured-verdict completion path."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth import create_agent_token
from app.models import Artifact, Project, Task, TaskStatus
from app.task_completion import (
    _PANEL_REVIEW_ARTIFACT_TYPE,
    _PANEL_REVIEW_REQUIRED_LENSES,
    _handle_panel_review_completion,
    _parse_panel_review_output,
)
from app.task_evaluators import evaluate_terminal_output
from testing.factories import make_project, make_task


def _panel_review_payload(
    *,
    overall_verdict: str = "APPROVE",
    drop_lens: str | None = None,
) -> dict:
    lenses = {
        "correctness": {
            "verdict": "APPROVE",
            "summary": "Logic and edge-case review passed.",
        },
        "security": {
            "verdict": "APPROVE",
            "summary": "No auth, injection, or secret-handling regressions.",
        },
        "code_quality": {
            "verdict": "APPROVE",
            "summary": "The diff follows existing patterns and keeps contracts aligned.",
        },
        "test_coverage": {
            "verdict": "APPROVE",
            "summary": "Changed behavior remains covered by targeted tests.",
        },
    }
    if drop_lens is not None:
        lenses.pop(drop_lens, None)
    return {
        "overall_verdict": overall_verdict,
        "summary": "All declared review lenses completed.",
        "investigated_files": [
            "agent/src/handlers/panel-review.ts",
            "admin-ui/src/components/TaskDetailDrawerMeta.tsx",
        ],
        "lenses": lenses,
    }


@pytest.fixture
def _quiet_broadcast(mocker):
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )


class TestParsePanelReviewOutput:
    def test_bare_json_object(self):
        payload = _panel_review_payload()
        out = _parse_panel_review_output(json.dumps(payload))
        assert out is not None
        assert out["overall_verdict"] == "APPROVE"
        assert out["investigated_files"] == payload["investigated_files"]
        assert tuple(out["lenses"]) == _PANEL_REVIEW_REQUIRED_LENSES

    def test_object_with_surrounding_text(self):
        body = f"Thinking...\n{json.dumps(_panel_review_payload())}\nDone."
        out = _parse_panel_review_output(body)
        assert out is not None
        assert out["lenses"]["security"]["summary"].startswith("No auth")

    def test_missing_declared_lens_returns_none(self):
        out = _parse_panel_review_output(
            json.dumps(_panel_review_payload(drop_lens="security"))
        )
        assert out is None


@pytest.mark.asyncio
async def test_panel_review_payload_schema_accepts_review_style_fields():
    from app.payload_schemas import validate_task_payload

    out = validate_task_payload(
        {
            "type": "panel_review",
            "pr_number": 77,
            "branch": "feature/panel-review",
            "base_branch": "automain",
            "original_prompt": "Deep review this PR from multiple lenses.",
        }
    )

    assert out["type"] == "panel_review"
    assert out["pr_number"] == 77
    assert out["branch"] == "feature/panel-review"


@pytest.mark.asyncio
async def test_panel_review_completion_persists_artifact_and_plan_output(
    db_session, _quiet_broadcast
):
    project = make_project(name="PanelReviewProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Panel review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "panel_review",
                "pr_number": 12,
                "branch": "feature/panel-review",
                "base_branch": "automain",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(_panel_review_payload(overall_verdict="REQUEST_CHANGES"))
    await _handle_panel_review_completion(task, logs, db_session)

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == _PANEL_REVIEW_ARTIFACT_TYPE
    assert json.loads(artifacts[0].logs)["overall_verdict"] == "REQUEST_CHANGES"

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["overall_verdict"] == "REQUEST_CHANGES"
    assert set(task.plan_output["lenses"]) == set(_PANEL_REVIEW_REQUIRED_LENSES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("logs", "reason_fragment"),
    [
        ("   ", "empty"),
        ('{"summary":"missing verdict","lenses":{}}', "structured verdict"),
        (
            json.dumps(_panel_review_payload(drop_lens="test_coverage")),
            "structured verdict",
        ),
    ],
)
async def test_panel_review_completion_invalid_output_fails_gate(
    db_session, _quiet_broadcast, logs: str, reason_fragment: str
):
    project = make_project(name="PanelReviewBad")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Panel review invalid",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "panel_review",
                "pr_number": 13,
                "branch": "feature/panel-review",
                "base_branch": "automain",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_panel_review_completion(task, logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.FAILED
    assert reason_fragment in (task.latest_status_message or "").lower()
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "structured-verdict"
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []


def test_panel_review_evaluator_resolves_to_structured_verdict():
    task = Task(
        payloads=[
            {
                "type": "panel_review",
                "pr_number": 21,
                "branch": "feature/panel-review",
                "base_branch": "automain",
            }
        ]
    )
    verdict = evaluate_terminal_output(task, json.dumps(_panel_review_payload()))
    assert verdict.passed is True
    assert verdict.strategy == "structured-verdict"


@pytest.mark.asyncio
async def test_manual_task_creation_accepts_panel_review_type(
    client: AsyncClient, db_session
):
    with (
        patch(
            "app.release_promotion._resolve_github_with_session",
            new_callable=AsyncMock,
            return_value=("tok", "acme", "panel-review-repo"),
        ),
        patch(
            "app.github.get_repo_default_branch",
            new_callable=AsyncMock,
            return_value="automain",
        ),
    ):
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/panel-review-repo",
                "title": "Panel review task",
                "prompt": "Run the deep panel review workflow.",
                "task_type": "panel_review",
                "pr_number": 88,
                "branch": "feature/panel-review",
            },
        )

    assert resp.status_code == 200, resp.text
    panel_review_tasks = [
        t
        for t in (await db_session.execute(select(Task))).scalars().all()
        if (t.first_payload or {}).get("type") == "panel_review"
    ]
    assert len(panel_review_tasks) == 1
    assert panel_review_tasks[0].first_payload["pr_number"] == 88
    assert panel_review_tasks[0].first_payload["original_prompt"] == (
        "Run the deep panel review workflow."
    )


@pytest.mark.asyncio
async def test_panel_review_status_endpoint_routes_via_registry_handler(
    client: AsyncClient, db_session, mocker
):
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )

    project = Project(name="PanelReviewEndpoint", user_id="test-user-panel")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "panel_review",
                "pr_number": 91,
                "branch": "feature/panel-review",
                "base_branch": "automain",
            }
        ],
        vm_instance_id="agent-vm-panel-review-endpoint",
        status=TaskStatus.IN_PROGRESS,
    )
    sibling = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        vm_instance_id="agent-vm-panel-review-sibling",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    db_session.add(sibling)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-panel-review-endpoint")
    resp = await client.post(
        "/api/agent/status",
        json={
            "task_id": str(task.id),
            "status": "COMPLETED",
            "logs": json.dumps(_panel_review_payload()),
        },
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == _PANEL_REVIEW_ARTIFACT_TYPE


def _resolve_panel_review_ts_path() -> "Path | None":
    """Locate ``agent/src/handlers/panel-review.ts`` across local + Docker test
    environments.

    Search order mirrors ``_resolve_prompt_ts_path`` in test_invariants.py:
      1. ``/agent/src/handlers/panel-review.ts`` (Docker volume-mount target used
         by ``scripts/test-backend.sh``).
      2. Host filesystem at ``../../agent/src/handlers/panel-review.ts`` from this
         test file (``backend/testing/`` → repo root).

    Returns the first existing path or ``None``. The CI backend Docker job does
    not mount ``agent/src`` (only the backend tree is in the build context), so
    the file is unreachable there — the caller must skip the file-content
    cross-check rather than raising FileNotFoundError.
    """
    candidates = [
        Path("/agent/src/handlers/panel-review.ts"),
        Path(__file__).resolve().parents[2]
        / "agent"
        / "src"
        / "handlers"
        / "panel-review.ts",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def test_panel_review_lenses_match_parallel_review_guidance_contract():
    """Static invariant: the backend lens contract mirrors the agent's fixed
    panel-review lens declaration, so the parser does not silently drift away
    from the executable workflow it expects to grade."""
    expected = ("correctness", "security", "code_quality", "test_coverage")
    assert _PANEL_REVIEW_REQUIRED_LENSES == expected

    lens_path = _resolve_panel_review_ts_path()
    if lens_path is None:
        pytest.skip("agent/src not available in this test environment")
    text = lens_path.read_text(encoding="utf-8")
    for lens_name in expected:
        assert f"id: '{lens_name}'" in text


def test_panel_review_handler_genuinely_fans_out_per_lens():
    """Static invariant: the agent handler runs a GENUINE multi-agent workflow —
    one independent ``runClaudeCode`` pass per declared lens that writes a
    lens-specific output file — rather than a single combined invocation.

    The agent vitest suite is not run in CI, so this run_shape=workflow guarantee
    must be enforced by a backend static scan too (precedent: the lens-contract
    scan above). If this handler ever regresses to a single combined pass, the
    declared workflow run-shape would silently become declarative-only again.
    """
    lens_path = _resolve_panel_review_ts_path()
    if lens_path is None:
        pytest.skip("agent/src not available in this test environment")
    text = lens_path.read_text(encoding="utf-8")

    # The handler iterates the declared lenses and invokes the model inside that
    # loop — the structural signature of genuine per-lens fan-out.
    assert "PANEL_REVIEW_LENSES.entries()" in text
    assert "for (const [index, lens] of PANEL_REVIEW_LENSES.entries())" in text
    assert "runClaudeCode(" in text

    # Each lens reads its OWN output channel (panel_review_lens_<id>), not one
    # shared combined verdict file.
    assert "panel_review_lens_" in text
    assert "panelReviewLensOutputType" in text

    # A deterministic code-side synthesis aggregates the per-lens passes into the
    # single structured object the backend gate consumes.
    assert "synthesizePanelReviewVerdict" in text

    # Fail-closed marker: a crashed or unusable lens must surface honestly.
    assert "panel_review_lens_failed" in text

    # Guard against regression to the old single combined verdict file written by
    # the model in one pass.
    assert "panel_review_verdict_" not in text
