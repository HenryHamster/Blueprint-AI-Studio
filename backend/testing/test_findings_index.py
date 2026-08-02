"""Modularity tripwire + smoke tests for findings services.

The read-only query surface (``lookup_findings_by_source_ref`` and
``query_findings_index``) lives in ``app.findings_index``. The mutating
``_shelve_findings_for_next_cycle`` recovery helper lives in
``app.findings_shelving``.
``research_completion`` re-exports each symbol so the seven external importers
that historically did ``from app.research_completion import ...`` keep working
(and so ``test_memo_exit_e2e`` can still monkeypatch
``app.research_completion.query_findings_index``).

This tripwire mirrors ``test_orchestrator_modularity.py``'s facade contract:

  1. Each extracted symbol is NOT re-defined at module top-level inside
     ``research_completion.py`` (re-export imports are allowed/expected).
  2. Each symbol IS importable from its canonical module and
     ``app.research_completion`` and resolves to the SAME object.
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

EXTRACTED_SYMBOL_MODULES = {
    "lookup_findings_by_source_ref": "app.findings_index",
    "query_findings_index": "app.findings_index",
    "_shelve_findings_for_next_cycle": "app.findings_shelving",
}
EXTRACTED_SYMBOLS = tuple(EXTRACTED_SYMBOL_MODULES)


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
            "their canonical findings modules. This shadows the implementation "
            "and re-grows the module.\n\nRe-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: keep each definition in its canonical findings module and "
            "re-export it from research_completion with an import."
        )


class TestExtractedSymbolsImportableFromBothModules:
    @pytest.mark.parametrize("symbol", EXTRACTED_SYMBOLS)
    def test_symbol_importable_and_identical(self, symbol: str):
        canonical_name = EXTRACTED_SYMBOL_MODULES[symbol]
        canonical = importlib.import_module(canonical_name)
        facade = importlib.import_module("app.research_completion")

        assert hasattr(canonical, symbol), (
            f"{canonical_name}.{symbol} is missing — the extraction "
            f"target lost a symbol."
        )
        assert hasattr(facade, symbol), (
            f"app.research_completion.{symbol} is no longer importable. "
            f"Add the re-export `from app.findings_index import {symbol}  "
            f"# noqa: F401`."
        )
        assert getattr(canonical, symbol) is getattr(facade, symbol), (
            f"app.research_completion.{symbol} is not the same object as "
            f"{canonical_name}.{symbol} — the facade must re-export, not "
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
        task.context = {
            "direction_id": "d-1",
            "research_findings_finalized": True,
        }
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
        # actionability_grade is exported verbatim; verification_basis is added
        # for consistency and defaults to pattern_inferred for legacy findings
        # that carry no such field.
        assert rows[0]["actionability_grade"] == "HIGH"
        assert rows[0]["verification_basis"] == "pattern_inferred"

    @pytest.mark.asyncio
    async def test_verification_basis_exported_when_present(self):
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        task = MagicMock()
        task.id = "t-2"
        task.status = TaskStatus.COMPLETED
        task.context = {
            "direction_id": "d-2",
            "research_findings_finalized": True,
        }
        task.created_at = None
        task.first_payload = {"type": "research"}
        task.plan_output = [
            {
                "title": "Finding B",
                "description": "desc",
                "actionability_grade": "MEDIUM",
                "verification_basis": "code_verified",
            }
        ]

        session = self._mock_session([task])
        rows = await query_findings_index(session, "proj-2")

        assert len(rows) == 1
        assert rows[0]["verification_basis"] == "code_verified"

    @pytest.mark.asyncio
    async def test_downgrade_metadata_exported_when_present(self):
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        task = MagicMock()
        task.id = "t-3"
        task.status = TaskStatus.COMPLETED
        task.context = {
            "direction_id": "d-3",
            "research_findings_finalized": True,
        }
        task.created_at = None
        task.first_payload = {"type": "research"}
        task.plan_output = [
            {
                "title": "Finding C",
                "description": "desc",
                "actionability_grade": "LOW",
                "verification_basis": "pattern_inferred",
                "verification_basis_downgraded_from": "code_verified",
                "verification_basis_downgrade_reason": (
                    "HEAD validation failed: 2 broken source refs, 1 ungrounded claim"
                ),
            }
        ]

        session = self._mock_session([task])
        rows = await query_findings_index(session, "proj-3")

        assert len(rows) == 1
        assert rows[0]["verification_basis"] == "pattern_inferred"
        assert rows[0]["verification_basis_downgraded_from"] == "code_verified"
        assert rows[0]["verification_basis_downgrade_reason"] == (
            "HEAD validation failed: 2 broken source refs, 1 ungrounded claim"
        )

    @pytest.mark.asyncio
    async def test_query_findings_index_includes_bounded_finding_challenge(self):
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        task = MagicMock()
        task.id = "t-ch1"
        task.status = TaskStatus.COMPLETED
        task.context = {
            "direction_id": "d-ch",
            "research_findings_finalized": True,
        }
        task.created_at = None
        task.first_payload = {"type": "research"}
        task.plan_output = [
            {
                "title": "Challenged Finding",
                "description": "desc",
                "actionability_grade": "HIGH",
                "finding_challenge": {
                    "challenged_boundary": "actionability",
                    "agreement": "agrees",
                    "second_opinion_verdict": "CONFIRMED",
                    "reason": "The challenger independently verified the finding and agrees it is actionable.",
                },
            }
        ]

        session = self._mock_session([task])
        rows = await query_findings_index(session, "proj-ch")

        assert len(rows) == 1
        row = rows[0]
        assert "challenge_summary" in row
        cs = row["challenge_summary"]
        assert cs["challenged_boundary"] == "actionability"
        assert cs["agreement"] == "agrees"
        assert cs["second_opinion_verdict"] == "CONFIRMED"
        assert "challenger independently verified" in cs["reason"]

    @pytest.mark.asyncio
    async def test_challenge_summary_omitted_when_absent(self):
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        task = MagicMock()
        task.id = "t-no-ch"
        task.status = TaskStatus.COMPLETED
        task.context = {
            "direction_id": "d-no-ch",
            "research_findings_finalized": True,
        }
        task.created_at = None
        task.first_payload = {"type": "research"}
        task.plan_output = [
            {"title": "Unchallenged Finding", "description": "desc", "actionability_grade": "MEDIUM"}
        ]

        session = self._mock_session([task])
        rows = await query_findings_index(session, "proj-no-ch")

        assert len(rows) == 1
        assert "challenge_summary" not in rows[0]

    @pytest.mark.asyncio
    async def test_challenge_summary_agreement_values(self):
        """Verify agrees/disagrees/indeterminate normalization."""
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        def _make_task(task_id, agreement_val):
            t = MagicMock()
            t.id = task_id
            t.status = TaskStatus.COMPLETED
            t.context = {
                "direction_id": "d-agr",
                "research_findings_finalized": True,
            }
            t.created_at = None
            t.first_payload = {"type": "research"}
            t.plan_output = [
                {
                    "title": "F",
                    "description": "d",
                    "actionability_grade": "MEDIUM",
                    "finding_challenge": {"agreement": agreement_val, "reason": "r"},
                }
            ]
            return t

        for val, expected in [
            ("agrees", "agrees"),
            ("disagrees", "disagrees"),
            ("AGREES", "agrees"),
            ("DISAGREES", "disagrees"),
            ("unknown", "indeterminate"),
            ("", "indeterminate"),
            (None, "indeterminate"),
        ]:
            task = _make_task(f"t-agr-{val}", val)
            session = self._mock_session([task])
            rows = await query_findings_index(session, "proj-agr")
            assert len(rows) == 1
            cs = rows[0]["challenge_summary"]
            assert cs["agreement"] == expected, f"agreement={val!r} → expected {expected!r}, got {cs['agreement']!r}"

    @pytest.mark.asyncio
    async def test_challenge_reason_truncated_to_200_chars(self):
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        long_reason = "x" * 300

        task = MagicMock()
        task.id = "t-trunc"
        task.status = TaskStatus.COMPLETED
        task.context = {
            "direction_id": "d-trunc",
            "research_findings_finalized": True,
        }
        task.created_at = None
        task.first_payload = {"type": "research"}
        task.plan_output = [
            {
                "title": "F",
                "description": "d",
                "actionability_grade": "MEDIUM",
                "finding_challenge": {
                    "agreement": "disagrees",
                    "reason": long_reason,
                },
            }
        ]

        session = self._mock_session([task])
        rows = await query_findings_index(session, "proj-trunc")
        assert len(rows) == 1
        assert len(rows[0]["challenge_summary"]["reason"]) == 200

    @pytest.mark.asyncio
    async def test_raw_completed_plan_output_is_not_indexed(self):
        """A pre-gate/legacy plan must not revive a rejected finding."""
        from app.findings_index import query_findings_index
        from app.models import TaskStatus

        task = MagicMock()
        task.id = "t-raw"
        task.status = TaskStatus.COMPLETED
        task.context = {"direction_id": "d-raw"}
        task.created_at = None
        task.first_payload = {"type": "research"}
        task.plan_output = [
            {
                "title": "Refuted raw finding",
                "description": "must remain dead",
                "_adversarial_verdict": "fail",
            }
        ]

        rows = await query_findings_index(self._mock_session([task]), "proj-raw")

        assert rows == []

    @pytest.mark.asyncio
    async def test_source_lookup_uses_same_finalized_gate(self):
        from app.findings_index import lookup_findings_by_source_ref
        from app.models import TaskStatus

        raw = MagicMock()
        raw.id = "t-source-raw"
        raw.status = TaskStatus.COMPLETED
        raw.context = {"direction_id": "d-source"}
        raw.created_at = None
        raw.first_payload = {"type": "research"}
        raw.plan_output = [
            {
                "title": "Rejected source finding",
                "sources": [{"type": "file", "ref": "backend/app/target.py"}],
            }
        ]
        accepted = MagicMock()
        accepted.id = "t-source-final"
        accepted.status = TaskStatus.COMPLETED
        accepted.context = {
            "direction_id": "d-source",
            "research_findings_finalized": True,
        }
        accepted.created_at = None
        accepted.first_payload = {"type": "research"}
        accepted.plan_output = [
            {
                "title": "Accepted source finding",
                "sources": [{"type": "file", "ref": "backend/app/target.py"}],
            }
        ]

        rows = await lookup_findings_by_source_ref(
            self._mock_session([raw, accepted]), "proj-source", "backend/app/target.py"
        )

        assert [row["finding_title"] for row in rows] == ["Accepted source finding"]
