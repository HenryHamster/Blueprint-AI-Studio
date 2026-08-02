"""Modularity tripwire for ``app.playbook_learning``.

The SR catch-rate trends subsystem (``compute_sr_catch_rate_trends`` plus its
helper constellation and threshold constants) was extracted to
``app.playbook_sr_metrics``. ``playbook_learning`` re-exports each extracted
symbol so existing ``from app.playbook_learning import X`` callers and tests
keep working without changes.

This tripwire enforces two halves of the facade contract for each
EXTRACTED_SYMBOLS entry:

  1. The symbol is NOT defined at module top-level inside
     ``playbook_learning.py`` (no ``def``/``async def``/``class``/top-level
     ``Name = ...`` assignment). Re-export imports are allowed and expected.
  2. The symbol IS still importable as ``from app.playbook_learning import X``,
     and the object reached via the facade IS the same object as the
     canonical sibling-module definition.

If a future PR moves more chunks out of ``playbook_learning`` into sibling
modules, extend EXTRACTED_SYMBOLS so the guard expands automatically.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


PLAYBOOK_LEARNING_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "playbook_learning.py")
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.playbook_sr_metrics": (
        # Trend / sample-size threshold constants
        "_MIN_SR_SAMPLE",
        "_MIN_SR_FINDINGS",
        "_MIN_DIRECTION_SR_SAMPLE",
        "_TREND_DELTA_THRESHOLD",
        "_TREND_Z_MIN",
        "_PRECISION_RECALL_QUADRANT_THRESHOLD",
        "_SR_SEVERITY_DELTA_THRESHOLDS",
        "_SR_FLOOR_THRESHOLD",
        "_FLOOR_MIN_CONSECUTIVE_WINDOWS",
        "_SR_ENGAGEMENT_THRESHOLD",
        "_SR_ENGAGEMENT_MIN_PASSES",
        # Alert title prefixes + composer
        "_SR_REGRESSION_ALERT_TITLE_PREFIX",
        "_SR_FLOOR_ALERT_TITLE_PREFIX",
        "_SR_QUADRANT_ALERT_TITLE_PREFIX",
        "_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX",
        "_SR_ALERT_TITLE_MAX_PARTS",
        "_format_sr_alert_title",
        # Category/code-type sets
        "_EXECUTION_EVIDENCE_CATEGORIES",
        "_SR_CODE_CHANGE_TYPES",
        "_EDGE_CATS",
        # Helper functions used inside compute_sr_catch_rate_trends
        "_compute_edges_stats",
        "_stats_for_tasks",
        "_sr_skip_stats",
        "_compute_cat_fp_stats",
        "_compute_cat_severity_calibration",
        "_task_prompt_fingerprint",
        "_compute_reasoning_engagement",
        "_collect_prompt_versions",
        "_percentile",
        "_compute_miss_latency_stats",
        "_is_trend_significant",
        # Public entry point
        "compute_sr_catch_rate_trends",
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


class TestExtractedSymbolsNotRedefinedInPlaybookLearning:
    """Tripwire — an extracted symbol must not be re-defined in playbook_learning.

    If this test fails, a recent change inside
    ``backend/app/playbook_learning.py`` added a top-level ``def`` / ``class`` /
    module-level assignment whose name collides with a symbol that already
    lives in ``app.playbook_sr_metrics``. That silently re-grows the monolith
    and shadows the canonical implementation. Either:

      - Move the new definition into the appropriate sibling module and
        re-export it from ``playbook_learning``, OR
      - If the symbol genuinely belongs back in ``playbook_learning`` (rare),
        remove it from ``EXTRACTED_SYMBOLS`` in this test and also delete the
        sibling-module definition so there is only one source of truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        with open(PLAYBOOK_LEARNING_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "playbook_learning.py re-defines symbols that were extracted to "
            "sibling modules. This shadows the canonical implementation and "
            "re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical sibling "
            "module and let playbook_learning re-export it via "
            "`from app.<module> import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromPlaybookLearning:
    """Tripwire — extracted symbols must remain importable via the facade.

    External callers (admin_digest, agent.py, autonomous_research,
    research_completion, digest_metrics, the playbook-learning test files,
    alerts tests, pipeline-integration tests) historically import these
    symbols from ``app.playbook_learning`` and many still do. Removing the
    re-export import would silently break those call sites.
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_playbook_learning(
        self, source_module: str, symbol: str
    ):
        learning = importlib.import_module("app.playbook_learning")
        canonical = importlib.import_module(source_module)

        assert hasattr(learning, symbol), (
            f"app.playbook_learning.{symbol} is no longer importable. Add the "
            f"re-export:\n    from {source_module} import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home for "
            f"this extracted symbol no longer defines it. Either restore the "
            f"definition there, or remove it from EXTRACTED_SYMBOLS in this "
            f"test if the extraction was reverted intentionally."
        )

        learning_obj = getattr(learning, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert learning_obj is canonical_obj, (
            f"app.playbook_learning.{symbol} is not the same object as "
            f"{source_module}.{symbol}. playbook_learning is shadowing the "
            f"canonical implementation — drop the local definition and rely "
            f"on the re-export."
        )
