"""Modularity tripwire for ``app.task_lifecycle``.

The task_lifecycle monolith is being broken up along responsibility seams
(the fdd07b88 decomposition plan). As stages are extracted into sibling
modules, this tripwire enforces two halves of the facade contract for each
``EXTRACTED_SYMBOLS`` entry:

  1. The symbol is NOT defined at module top-level inside
     ``task_lifecycle.py`` (no ``def`` / ``async def`` / ``class`` /
     top-level ``Name = ...`` assignment). Re-export imports are allowed.
  2. The symbol IS still importable as
     ``from app.task_lifecycle import X`` and is the identical object as
     the one in its canonical leaf module.

``EXTRACTED_SYMBOLS`` is initially EMPTY — S1 and later extraction steps
will append entries as they move code. Keep this file well under 1500 lines
so no ``module_size_limits.json`` entry is needed.

Modeled on ``test_orchestrator_modularity.py`` and the
``TestRequeueExtractionTripwire`` class in ``test_task_lifecycle.py``.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


TASK_LIFECYCLE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "task_lifecycle.py")
)

# Maps leaf module name -> tuple of symbol names extracted there.
# S1 moved the post-commit exception types to app.post_commit_errors; S5
# extracted the review request-changes stage to app.review_request_changes;
# S2-S12 will append further entries as they move code out of task_lifecycle.py.
EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.post_commit_errors": ("PostCommitLocked", "PostCommitFinalCommitFailed"),
    "app.review_request_changes": ("run_request_changes",),
}


def _flatten_extracted_symbols() -> list[tuple[str, str]]:
    """Return (leaf_module, symbol_name) pairs from EXTRACTED_SYMBOLS."""
    return [(module, name) for module, names in EXTRACTED_SYMBOLS.items() for name in names]


def _top_level_definitions(source: str) -> dict[str, str]:
    """Return {symbol_name: definition_kind} for module top-level defs.

    Re-export imports (``from X import Y``) and aliased imports
    (``import X as Y``) are intentionally excluded — only brand-new
    definitions that would shadow the canonical home are flagged.
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


class TestExtractedSymbolsNotRedefinedInTaskLifecycle:
    """Tripwire — an extracted symbol must not be re-defined in task_lifecycle.

    If this test fails, a recent change inside
    ``backend/app/task_lifecycle.py`` added a top-level ``def`` /
    ``class`` / module-level assignment whose name collides with a symbol
    that already lives in one of the sibling leaf modules. Either:

      - Move the new definition into the appropriate sibling module and
        re-export it from task_lifecycle, OR
      - If the symbol genuinely belongs back in task_lifecycle (rare),
        remove it from ``EXTRACTED_SYMBOLS`` in this test AND delete the
        sibling-module definition so there is exactly one source of truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        pairs = _flatten_extracted_symbols()
        if not pairs:
            # No extractions yet — trivially passes.
            return

        with open(TASK_LIFECYCLE_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in pairs:
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "task_lifecycle.py re-defines symbols that were extracted to "
            "leaf sibling modules. This shadows the canonical implementation "
            "and re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical leaf "
            "module and let task_lifecycle re-export it via "
            "`from app.<module> import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromTaskLifecycle:
    """Tripwire — extracted symbols must remain importable via the facade.

    Existing callers import these symbols from ``app.task_lifecycle``; removing
    the re-export would silently break them. This parametrized test catches
    any re-export omission immediately.
    """

    @pytest.mark.parametrize(
        "leaf_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_task_lifecycle(self, leaf_module: str, symbol: str):
        facade = importlib.import_module("app.task_lifecycle")
        canonical = importlib.import_module(leaf_module)

        assert hasattr(facade, symbol), (
            f"app.task_lifecycle.{symbol} is no longer importable. "
            f"Add the re-export:\n"
            f"    from {leaf_module} import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"{leaf_module}.{symbol} is missing — the canonical home "
            f"for this extracted symbol no longer defines it. Either "
            f"restore the definition there, or remove it from "
            f"EXTRACTED_SYMBOLS in this test if the extraction was "
            f"reverted intentionally."
        )

        facade_obj = getattr(facade, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert facade_obj is canonical_obj, (
            f"app.task_lifecycle.{symbol} is not the same object as "
            f"{leaf_module}.{symbol}. The facade is shadowing the "
            f"canonical implementation — drop the local definition and "
            f"rely on the re-export."
        )
