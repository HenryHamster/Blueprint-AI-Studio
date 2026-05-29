"""Modularity tripwire for ``app.autonomous_research``.

The autonomous-research monolith is being broken up along clean
responsibility seams — the failure-driven investigation / remediation
spawners (direction-hold investigations, task-failure investigations,
review-convergence investigations, fingerprint-audit-system inference,
residual-fingerprint remediation, and their shared recent-investigation
dedup gate) now live in ``app.autonomous_research_investigations``. The
``autonomous_research`` module re-exports each extracted symbol so
existing ``from app.autonomous_research import X`` callers (tests and
runtime code) keep working unchanged.

This tripwire enforces two halves of the facade contract for each
EXTRACTED_SYMBOLS entry, mirroring
``test_orchestrator_modularity.py``:

  1. The symbol is NOT defined at module top-level inside
     ``autonomous_research.py`` (no ``def`` / ``async def`` / ``class`` /
     top-level ``Name = ...`` assignment). Re-export imports are
     allowed and expected.
  2. The symbol IS still importable as
     ``from app.autonomous_research import X``.

Future extractions should extend EXTRACTED_SYMBOLS with the names they
moved so the seam stays guarded automatically.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


AUTONOMOUS_RESEARCH_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "autonomous_research.py")
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.autonomous_research_investigations": (
        "_MAX_ACTIVE_INVESTIGATIONS_PER_DIRECTION",
        "_recent_investigation_exists",
        "_spawn_direction_hold_investigation",
        "_spawn_task_failure_investigation",
        "_spawn_review_convergence_investigation",
        "_infer_fingerprint_audit_system",
        "_spawn_residual_fingerprint_remediation",
    ),
}


def _flatten_extracted_symbols() -> list[tuple[str, str]]:
    """Return (source_module, symbol_name) pairs from EXTRACTED_SYMBOLS."""
    pairs: list[tuple[str, str]] = []
    for module, names in EXTRACTED_SYMBOLS.items():
        for name in names:
            pairs.append((module, name))
    return pairs


def _top_level_definitions(source: str) -> dict[str, str]:
    """Return {symbol_name: definition_kind} for module top-level defs.

    Kinds: "function", "async function", "class", "assignment".
    Re-export imports (``from X import Y``) and aliased imports
    (``import X as Y``) are intentionally excluded — the tripwire only
    cares about brand-new definitions that shadow the canonical home.
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


class TestExtractedSymbolsNotRedefinedInAutonomousResearch:
    """Tripwire — an extracted symbol must not be re-defined in the original.

    If this test fails, a recent change inside
    ``backend/app/autonomous_research.py`` added a top-level ``def`` /
    ``class`` / module-level assignment whose name collides with a
    symbol that already lives in
    ``app.autonomous_research_investigations``. That silently re-grows
    the monolith and shadows the canonical implementation. Either:

      - Move the new definition into
        ``app.autonomous_research_investigations`` and re-export it from
        ``autonomous_research``, OR
      - If the symbol genuinely belongs back in ``autonomous_research``
        (rare), remove it from ``EXTRACTED_SYMBOLS`` in this test and
        also delete the sibling-module definition so there is only one
        source of truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        with open(AUTONOMOUS_RESEARCH_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "autonomous_research.py re-defines symbols that were extracted "
            "to a sibling module. This shadows the canonical implementation "
            "and re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical sibling "
            "module and let autonomous_research re-export it via "
            "`from app.<module> import <name>  # noqa: E402,F401`."
        )


class TestExtractedSymbolsImportableFromAutonomousResearch:
    """Tripwire — extracted symbols must remain importable via the facade.

    External callers (tests and runtime code) historically imported
    these symbols from ``app.autonomous_research`` and many still do.
    Removing the re-export import would silently break those call sites.
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_autonomous_research(
        self, source_module: str, symbol: str
    ):
        autonomous_research = importlib.import_module("app.autonomous_research")
        canonical = importlib.import_module(source_module)

        assert hasattr(autonomous_research, symbol), (
            f"app.autonomous_research.{symbol} is no longer importable. "
            f"Add the re-export:\n"
            f"    from {source_module} import {symbol}  # noqa: E402,F401"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home "
            f"for this extracted symbol no longer defines it. Either "
            f"restore the definition there, or remove it from "
            f"EXTRACTED_SYMBOLS in this test if the extraction was "
            f"reverted intentionally."
        )

        facade_obj = getattr(autonomous_research, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert facade_obj is canonical_obj, (
            f"app.autonomous_research.{symbol} is not the same object as "
            f"{source_module}.{symbol}. autonomous_research is shadowing "
            f"the canonical implementation — drop the local definition "
            f"and rely on the re-export."
        )


class TestAutonomousResearchInvestigationsModuleExists:
    """The extracted sibling module must exist and be importable."""

    def test_module_importable(self):
        mod = importlib.import_module("app.autonomous_research_investigations")
        assert mod is not None
