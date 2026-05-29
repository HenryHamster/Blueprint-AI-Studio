"""Modularity tripwire + smoke tests for ``app.findings_index``.

The read-only findings-index query surface (``lookup_findings_by_source_ref``,
``query_findings_index``, ``_shelve_findings_for_next_cycle``) was extracted
from ``app.research_completion`` into ``app.findings_index``.
``research_completion`` re-exports each symbol so the seven external importers
that historically did ``from app.research_completion import ...`` keep working
(and so ``test_memo_exit_e2e`` can still monkeypatch
``app.research_completion.query_findings_index``).

This tripwire mirrors ``test_orchestrator_modularity.py``'s facade contract:

  1. Each extracted symbol is NOT re-defined at module top-level inside
     ``research_completion.py`` (re-export imports are allowed/expected).
  2. Each symbol IS importable from BOTH ``app.research_completion`` and
     ``app.findings_index`` and resolves to the SAME object.
"""

from __future__ import annotations

import ast
import importlib
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

RESEARCH_COMPLETION_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "research_completion.py")
)

EXTRACTED_SYMBOLS = (
    "lookup_findings_by_source_ref",
    "query_findings_index",
    "_shelve_findings_for_next_cycle",
)


def _top_level_definitions(source: str) -> dict[str, str]:
    """Return {symbol_name: definition_kind} for module top-level defs.

    Re-export imports (``from X import Y``) are intentionally excluded — the
    tripwire only cares about brand-new definitions that shadow the canonical
    home in ``app.findings_index``.
    """
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            found.setdefault(node.name, "function")
        elif isinstance(node, ast.AsyncFunctionDef):
            found.setdefault(node.name, "async function")
        elif isinstance(node, ast.ClassDef):
            found.setdefault(node.name, "class")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, "assignment")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.setdefault(node.target.id, "assignment")
    return found


class TestExtractedSymbolsNotRedefinedInResearchCompletion:
    def test_no_extracted_symbol_is_redefined(self):
        with open(RESEARCH_COMPLETION_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations = [
            f"  {name} ({defs[name]})"
            for name in EXTRACTED_SYMBOLS
            if name in defs
        ]
        assert not violations, (
            "research_completion.py re-defines symbols extracted to "
            "app.findings_index. This shadows the canonical implementation "
            "and re-grows the module.\n\nRe-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: keep the definition in app.findings_index and "
            "re-export it via `from app.findings_index import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromBothModules:
    @pytest.mark.parametrize("symbol", EXTRACTED_SYMBOLS)
    def test_symbol_importable_and_identical(self, symbol: str):
        canonical = importlib.import_module("app.findings_index")
        facade = importlib.import_module("app.research_completion")

        assert hasattr(canonical, symbol), (
            f"app.findings_index.{symbol} is missing — the extraction "
            f"target lost a symbol."
        )
        assert hasattr(facade, symbol), (
            f"app.research_completion.{symbol} is no longer importable. "
            f"Add the re-export `from app.findings_index import {symbol}  "
            f"# noqa: F401`."
        )
        assert getattr(canonical, symbol) is getattr(facade, symbol), (
            f"app.research_completion.{symbol} is not the same object as "
            f"app.findings_index.{symbol} — the facade must re-export, not "
            f"re-implement."
        )


class TestQueryFindingsIndexSmoke:
    """Smoke test exercising the canonical module directly (not via facade)."""

    def _mock_session(self, tasks):
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = tasks
        session = AsyncMock()
        session.execute.return_value = mock_result
        return session

    @pytest.mark.asyncio
    async def test_returns_rows_from_completed_research_task(self):
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        task = MagicMock()
        task.id = "t-1"
        task.status = TaskStatus.COMPLETED
        task.context = {"direction_id": "d-1"}
        task.created_at = None
        task.first_payload = {"type": "research"}
        task.plan_output = [
            {"title": "Finding A", "description": "desc", "actionability_grade": "HIGH"}
        ]

        session = self._mock_session([task])
        rows = await query_findings_index(session, "proj-1")

        assert len(rows) == 1
        assert rows[0]["finding_title"] == "Finding A"
        assert rows[0]["direction_id"] == "d-1"
