"""Modularity tripwire for ``app.routers.admin_digest``.

``admin_digest`` has been an oversized cluster module for a while. As
parts of it are peeled into sibling modules (``app.pipeline_health``,
``app.digest_metrics``, ``app.viewport_debt``, ``app.efficiency_snapshot``,
``app.sr_blind_spots``, ``app.sr_analytics``, ``app.research_actionability``,
``app.anomaly_detection`` …), the router still re-exports each extracted
symbol so existing ``from app.routers.admin_digest import X`` callers
keep working. PR #5877 demonstrated the failure mode: 14+ endpoint
functions were moved without re-exports, silently breaking downstream
imports. This tripwire fails loudly if either half of that contract
gets violated:

  1. The symbol is NOT re-defined at module top-level inside
     ``backend/app/routers/admin_digest.py`` (no ``def``/``async def``/
     ``class``/top-level ``Name = ...`` assignment). Re-export imports
     (``from app.<module> import X``) are allowed and expected.
  2. The symbol IS still importable as
     ``from app.routers.admin_digest import X`` and resolves to the
     canonical sibling-module object — admin_digest must not be
     shadowing the canonical implementation with a stale local copy.

Every cluster-extraction PR that moves a new symbol out of
``admin_digest`` must extend ``EXTRACTED_SYMBOLS`` below with the
(canonical_module, symbol_name) pair. That hooks the new seam into
the tripwire automatically — future regressions are caught at test
time instead of becoming runtime ImportErrors in production.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


ADMIN_DIGEST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "routers", "admin_digest.py")
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.pipeline_health": (
        "_LONE_CHECKER_ALERT_TITLE",
        "_LONE_CHECKER_MIN_CATEGORIES",
        "_SR_CATCH_RATE_MIN_FINDINGS",
        "_SR_CATCH_RATE_TARGET_ALERT_PREFIX",
        "_SR_CATCH_RATE_TARGET_MIN_WINDOWS",
        "_evaluate_lone_checker_alert",
        "_evaluate_sr_catch_rate_targets",
        "compute_pipeline_health_summary",
    ),
    "app.digest_metrics": (
        "_CPM_RETIREMENT_MULTIPLIER",
        "_compute_edges_considered_metrics",
        "_compute_layer_coverage_by_class",
        "_compute_regression_case_summary",
        "_compute_symptom_layers_by_class",
        "compute_digest_trend_history",
        "compute_failure_grouping",
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


class TestExtractedSymbolsNotRedefinedInAdminDigest:
    """Tripwire — an extracted symbol must not be re-defined in admin_digest.

    If this test fails, a recent change inside
    ``backend/app/routers/admin_digest.py`` added a top-level
    ``def`` / ``class`` / module-level assignment whose name collides
    with a symbol that already lives in one of the sibling modules. That
    silently re-grows the monolith and shadows the canonical
    implementation. Either:

      - Move the new definition into the appropriate sibling module and
        re-export it from admin_digest, OR
      - If the symbol genuinely belongs back inside admin_digest (rare),
        remove it from ``EXTRACTED_SYMBOLS`` in this test and also delete
        the sibling-module definition so there is only one source of truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        with open(ADMIN_DIGEST_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "app/routers/admin_digest.py re-defines symbols that were "
            "extracted to sibling modules. This shadows the canonical "
            "implementation and re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical sibling "
            "module and let admin_digest re-export it via "
            "`from app.<module> import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromAdminDigest:
    """Tripwire — extracted symbols must remain importable via the facade.

    External callers (the test suite, other routers, tooling) have
    historically imported these symbols from ``app.routers.admin_digest``
    and many still do. Removing the re-export import would silently
    break those call sites. PR #5877 was the precedent that demonstrates
    this failure mode at scale — this test exists to keep it from
    recurring.
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_admin_digest(self, source_module: str, symbol: str):
        admin_digest = importlib.import_module("app.routers.admin_digest")
        canonical = importlib.import_module(source_module)

        assert hasattr(admin_digest, symbol), (
            f"app.routers.admin_digest.{symbol} is no longer importable. "
            f"Add the re-export at the top of admin_digest.py:\n"
            f"    from {source_module} import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home "
            f"for this extracted symbol no longer defines it. Either "
            f"restore the definition there, or remove it from "
            f"EXTRACTED_SYMBOLS in this test if the extraction was "
            f"reverted intentionally."
        )

        admin_digest_obj = getattr(admin_digest, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert admin_digest_obj is canonical_obj, (
            f"app.routers.admin_digest.{symbol} is not the same object "
            f"as {source_module}.{symbol}. admin_digest is shadowing the "
            f"canonical implementation — drop the local definition and "
            f"rely on the re-export."
        )
