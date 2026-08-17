"""Import contract for the extracted admin approval orchestration."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks

from app.models import Task, TaskStatus


_APP_ROOT = Path(__file__).parents[1] / "app"
_APPROVAL_NAMES = (
    "_apply_approval_mutation",
    "_handle_rollup_pr_approval",
    "_maybe_spawn_rollup_smoketest",
    "_handle_task_pr_approval",
    "_merge_critical_pr",
    "_spawn_review_for_approved_task",
)


def test_admin_mutation_facade_reexports_canonical_approval_helpers():
    """Existing facade imports resolve to the sole canonical implementation."""
    from app import admin_task_approvals as canonical
    from app import admin_task_mutations as facade

    for name in _APPROVAL_NAMES:
        assert getattr(facade, name) is getattr(canonical, name), (
            f"app.admin_task_mutations.{name} must re-export the canonical "
            "approval helper by identity"
        )

    facade_tree = ast.parse(
        (_APP_ROOT / "admin_task_mutations.py").read_text(encoding="utf-8")
    )
    facade_definitions = {
        node.name
        for node in facade_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert not facade_definitions.intersection(_APPROVAL_NAMES), (
        "admin_task_mutations.py must not retain a duplicate approval definition"
    )


@pytest.mark.asyncio
async def test_modify_task_uses_behavioral_facade_patch_seam(monkeypatch):
    """Approval orchestration resolves patches installed on the public facade."""
    from app import admin_task_mutations as facade

    task = Task(
        project_id=uuid.uuid4(),
        title="approval facade seam",
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
    )
    decision = object()
    decide = AsyncMock(return_value=(task, decision, {}))
    execute = AsyncMock(return_value=(task, {}))
    monkeypatch.setattr(facade, "decide_operator_approval", decide, raising=False)
    monkeypatch.setattr(facade, "execute_approval_followup", execute, raising=False)
    monkeypatch.setattr(facade, "_apply_status_mutation", AsyncMock())
    monkeypatch.setattr(facade, "_apply_simple_mutations", AsyncMock())
    monkeypatch.setattr(facade, "_apply_depends_on_mutation", AsyncMock())
    monkeypatch.setattr(facade, "_commit_and_broadcast", AsyncMock())
    session = object()

    result = await facade.modify_task_apply(
        task,
        {"requires_approval": False},
        BackgroundTasks(),
        session,
    )

    decide.assert_awaited_once_with(
        task.id, False, session, modified_by="USER"
    )
    execute.assert_awaited_once_with(decision, session)
    assert result["id"] == str(task.id)
