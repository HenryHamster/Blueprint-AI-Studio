"""Modularity tripwire for ``app.task_completion``.

``task_completion`` is in the middle of a split — the self-audit completion
section now lives in ``task_completion_self_audit``. The facade re-exports each
moved symbol so existing ``from app.task_completion import X`` callers keep
working, but nothing in the suite has been preventing a future PR from
re-introducing one of those symbols as a brand-new definition inside
``task_completion.py`` — which would silently re-grow the monolith and shadow
the canonical implementation in the sibling module.

(The follow-up / investigation subsystem that formerly lived in
``task_completion_investigation`` was removed with the self-repair apparatus,
so its symbols are no longer part of the facade contract.)

This tripwire enforces two halves of the facade contract for each
EXTRACTED_SYMBOLS entry:

  1. The symbol is NOT defined at module top-level inside
     ``task_completion.py`` (no ``def``/``async def``/``class``/top-level
     ``Name = ...`` assignment). Re-export imports are allowed and expected.
  2. The symbol IS still importable as
     ``from app.task_completion import X``.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


TASK_COMPLETION_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "task_completion.py")
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.task_completion_self_audit": (
        "_AUDIT_CLEARABLE_MODES",
        "_MODE_STRICTNESS",
        "_SELF_AUDIT_COMPONENT_KEYS",
        "_SELF_AUDIT_PROPAGATED_MODES",
        "_SHIP_RECOVERY_THRESHOLD",
        "_VALID_SELF_AUDIT_MODES",
        "_handle_self_audit_completion",
        "_is_recent_strategic_mode_lock",
        "_maybe_auto_clear_direction_mode",
        "_normalize_self_audit_finding",
        "_parse_self_audit_findings",
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


class TestExtractedSymbolsNotRedefinedInTaskCompletion:
    """Tripwire — an extracted symbol must not be re-defined in task_completion.

    If this test fails, a recent change inside
    ``backend/app/task_completion.py`` added a top-level ``def`` / ``class``
    / module-level assignment whose name collides with a symbol that already
    lives in one of the sibling modules. That silently re-grows the monolith
    and shadows the canonical implementation. Either:

      - Move the new definition into the appropriate sibling module and
        re-export it from ``task_completion``, OR
      - If the symbol genuinely belongs back in ``task_completion`` (rare),
        remove it from ``EXTRACTED_SYMBOLS`` in this test and also delete
        the sibling-module definition so there is only one source of truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        with open(TASK_COMPLETION_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "task_completion.py re-defines symbols that were extracted to "
            "sibling modules. This shadows the canonical implementation and "
            "re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical sibling "
            "module and let task_completion re-export it via "
            "`from app.<module> import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromTaskCompletion:
    """Tripwire — extracted symbols must remain importable via the facade.

    External callers (task_lifecycle, test_task_completion, test_self_audit)
    historically imported these symbols from ``app.task_completion`` and many
    still do. Removing the re-export import would silently break those call
    sites.
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_task_completion(
        self, source_module: str, symbol: str
    ):
        task_completion = importlib.import_module("app.task_completion")
        canonical = importlib.import_module(source_module)

        assert hasattr(task_completion, symbol), (
            f"app.task_completion.{symbol} is no longer importable. "
            f"Add the re-export:\n"
            f"    from {source_module} import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home for "
            f"this extracted symbol no longer defines it. Either restore the "
            f"definition there, or remove it from EXTRACTED_SYMBOLS in this "
            f"test if the extraction was reverted intentionally."
        )

        facade_obj = getattr(task_completion, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert facade_obj is canonical_obj, (
            f"app.task_completion.{symbol} is not the same object as "
            f"{source_module}.{symbol}. The facade is shadowing the canonical "
            f"implementation — drop the local definition and rely on the "
            f"re-export."
        )
