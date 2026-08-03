"""Import contract for the extracted admin approval orchestration."""

from __future__ import annotations

import ast
from pathlib import Path


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
