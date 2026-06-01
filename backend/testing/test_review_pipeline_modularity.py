"""Modularity tripwire for ``app.review_pipeline``.

The review-pipeline module is kept under its size cap by extracting the
review<->fix outer-loop convergence machinery — the scope-pressure /
narrow-focus threshold constants and prompt prefixes, the pipeline-task
staleness check, and the terminal cap-exhaustion handler — into
``app.review_loop_convergence``. The pipeline re-exports each extracted
symbol so existing ``from app.review_pipeline import X`` callers (the
pipeline-task spawner, rollup_management, admin-digest router, and the
review-pipeline / outer-loop-convergence test suites) keep working
unchanged.

This tripwire enforces two halves of the facade contract for each
EXTRACTED_SYMBOLS entry:

  1. The symbol is NOT defined at module top-level inside
     ``review_pipeline.py`` (no ``def`` / ``async def`` / ``class`` /
     top-level ``Name = ...`` assignment). Re-export imports are
     allowed and expected.
  2. The symbol IS still importable as
     ``from app.review_pipeline import X`` and is the SAME object as the
     canonical definition in ``app.review_loop_convergence``.

Future extractions should extend EXTRACTED_SYMBOLS with the names they
moved so the seam stays guarded automatically.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


REVIEW_PIPELINE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "review_pipeline.py")
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.review_loop_convergence": (
        # Scope-pressure / narrow-focus thresholds + prompt prefixes.
        "MID_BUDGET_NARROW_FOCUS_THRESHOLD",
        "SCOPE_PRESSURE_HARD_CAP_PERCENTAGE",
        "SCOPE_PRESSURE_HARD_CAP_REMAINING",
        "SCOPE_PRESSURE_HARD_CAP_REMAINING_LARGE",
        "SCOPE_PRESSURE_WARN_THRESHOLD",
        "_CHURN_NARROW_FOCUS_PREFIX",
        "_EARLY_REPEATING_ISSUES_PREFIX",
        "_MID_BUDGET_NARROW_FOCUS_PREFIX",
        "_SCOPE_PRESSURE_HARD_CAP_PREFIX",
        # Pipeline-task staleness check.
        "PIPELINE_TASK_STALE_MINUTES",
        "_is_pipeline_task_stale",
        # Terminal cap-exhaustion handler (also a monkeypatch target on
        # app.review_pipeline in the pipeline tests).
        "_handle_cap_exhaustion",
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


class TestExtractedSymbolsNotRedefinedInReviewPipeline:
    """Tripwire — an extracted symbol must not be re-defined in the facade.

    If this test fails, a recent change inside
    ``backend/app/review_pipeline.py`` added a top-level ``def`` /
    ``class`` / module-level assignment whose name collides with a symbol
    that already lives in ``app.review_loop_convergence``. That silently
    re-grows the module and shadows the canonical implementation. Either:

      - Move the new definition into ``app.review_loop_convergence`` and
        re-export it from review_pipeline, OR
      - If the symbol genuinely belongs back in review_pipeline (rare),
        remove it from ``EXTRACTED_SYMBOLS`` in this test and also delete
        the sibling-module definition so there is only one source of
        truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        with open(REVIEW_PIPELINE_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "review_pipeline.py re-defines symbols that were extracted to "
            "app.review_loop_convergence. This shadows the canonical "
            "implementation and re-grows the module.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into review_loop_convergence "
            "and let review_pipeline re-export it via "
            "`from app.review_loop_convergence import <name>`."
        )


class TestExtractedSymbolsImportableFromReviewPipeline:
    """Tripwire — extracted symbols must remain importable via the facade.

    External callers (the pipeline-task spawner, rollup_management, the
    admin-digest router, and the pipeline / outer-loop-convergence test
    suites) import these symbols from ``app.review_pipeline``. Removing the
    re-export import would silently break those call sites — and the cap
    handler is a monkeypatch target on app.review_pipeline.
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_review_pipeline(
        self, source_module: str, symbol: str
    ):
        review_pipeline = importlib.import_module("app.review_pipeline")
        canonical = importlib.import_module(source_module)

        assert hasattr(review_pipeline, symbol), (
            f"app.review_pipeline.{symbol} is no longer importable. "
            f"Add the re-export:\n"
            f"    from {source_module} import {symbol}"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home "
            f"for this extracted symbol no longer defines it. Either "
            f"restore the definition there, or remove it from "
            f"EXTRACTED_SYMBOLS in this test if the extraction was "
            f"reverted intentionally."
        )

        review_pipeline_obj = getattr(review_pipeline, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert review_pipeline_obj is canonical_obj, (
            f"app.review_pipeline.{symbol} is not the same object as "
            f"{source_module}.{symbol}. review_pipeline is shadowing the "
            f"canonical implementation — drop the local definition and rely "
            f"on the re-export."
        )


class TestMaxReviewFixIterationsStaysInFacade:
    """``MAX_REVIEW_FIX_ITERATIONS`` is CI-pinned to app.review_pipeline.

    A dedicated invariant test imports it from app.review_pipeline and
    asserts it equals 16. It must stay defined IN review_pipeline (not
    extracted), so guard that here too.
    """

    def test_constant_defined_in_review_pipeline(self):
        with open(REVIEW_PIPELINE_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)
        assert defs.get("MAX_REVIEW_FIX_ITERATIONS") == "assignment", (
            "MAX_REVIEW_FIX_ITERATIONS must remain a top-level definition in "
            "review_pipeline.py — it is CI-pinned and imported from "
            "app.review_pipeline by the invariant suite."
        )
        review_pipeline = importlib.import_module("app.review_pipeline")
        assert review_pipeline.MAX_REVIEW_FIX_ITERATIONS == 16


class TestReviewLoopConvergenceModuleExists:
    """The extracted sibling module must exist and be importable."""

    def test_module_importable(self):
        mod = importlib.import_module("app.review_loop_convergence")
        assert mod is not None
