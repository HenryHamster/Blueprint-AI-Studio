"""Modularity tripwire for ``app.pipeline_regression_dispatcher``.

The dispatcher is in the middle of a multi-cluster split — chunks of its
former bulk now live in sibling modules (``regression_dispatcher_metrics``,
``regression_layer_classifier``, ``dispatcher_formatters``,
``pipeline_health``). The dispatcher re-exports each extracted symbol so
existing ``from app.pipeline_regression_dispatcher import X`` callers keep
working, but nothing in the test suite has been preventing a future PR from
re-introducing one of those symbols as a brand-new definition inside the
dispatcher — which would silently re-grow the monolith and shadow the
canonical implementation in the sibling module.

This tripwire enforces two halves of the facade contract for each
EXTRACTED_SYMBOLS entry:

  1. The symbol is NOT defined at module top-level inside
     ``pipeline_regression_dispatcher.py`` (no ``def``/``async def``/
     ``class``/top-level ``Name = ...`` assignment). Re-export imports are
     allowed and expected.
  2. The symbol IS still importable as
     ``from app.pipeline_regression_dispatcher import X``.

When the parallel "extract observation/path-classifier" and "move pure
layer-compute helpers" tasks (and the later signal-evaluator / recovery /
spawn / watchdog / queries clusters) land, each PR should extend
EXTRACTED_SYMBOLS with the names it moved. The tripwire then guards the
new seam automatically.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


DISPATCHER_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "app", "pipeline_regression_dispatcher.py"
    )
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.regression_dispatcher_metrics": (
        "compute_mtti_p50_sec",
        "_layers_targeted_union",
        "compute_untargeted_layers",
        "compute_class_layer_defense_counts",
    ),
    "app.regression_layer_classifier": (
        "LAYER_DISPLAY_ORDER",
        "aggregate_targeted_layers",
        "is_recurring_cross_layer_class",
    ),
    "app.dispatcher_formatters": (
        "_CROSS_CASE_FINGERPRINT_LOOKBACK",
        "_SR_FP_RATE_FLOOR",
        "_extract_evaluating_since",
        "_extract_recurrence_cycles",
        "_format_bundle_signal_lines",
        "_format_cross_case_fingerprint_prompt",
        "_format_cross_layer_prompt",
        "_format_dispatcher_duration",
        "_format_fragile_recurrence_meta_prompt",
        "_format_layer_concentration_prompt",
        "_format_prior_attempt_context",
        "_format_prior_verdict_context",
        "_format_recurrence_meta_prompt",
        "_format_recurring_class_prompt",
        "_format_reviewer_sla_stall_prompt",
        "_format_signal_lines",
        "_format_sr_fp_signal_lines",
        "_format_strategic_stall_prompt",
        "_format_theme_layer_meta_prompt",
        "_is_fragile_recurrence",
        "_parse_dispatcher_dt",
        "_parse_iso_timestamp",
        "_parse_signal_timestamp",
    ),
    "app.pipeline_health": (
        "_LONE_CHECKER_ALERT_TITLE",
        "_evaluate_lone_checker_alert",
        "_evaluate_sr_catch_rate_targets",
        "compute_pipeline_health_summary",
    ),
    "app.regression_recovery_evaluators": (
        "RegressionClassShape",
        "SPAWNABLE_REGRESSION_CLASS_SHAPES",
        "_recover_agent_vm_resource",
        "_recover_cross_case_fingerprint",
        "_recover_fix_loop_divergence_fingerprint",
        "_recover_layer_concentration",
        "_recover_observation_alert_sla",
        "_recover_prevention_low",
        "_recover_recurring_class",
        "_recover_reviewer_sla_stall",
        "_recover_sr_below_floor",
        "_recover_sr_false_attestation",
        "_recover_sr_pass_engagement_thinning",
        "_recover_strategic_cycle_stall",
        "_recover_viewport_coverage_drop",
        "_recover_viewport_regression",
        "match_spawnable_shape",
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


class TestExtractedSymbolsNotRedefinedInDispatcher:
    """Tripwire — an extracted symbol must not be re-defined in dispatcher.

    If this test fails, a recent change inside
    ``backend/app/pipeline_regression_dispatcher.py`` added a top-level
    ``def`` / ``class`` / module-level assignment whose name collides with
    a symbol that already lives in one of the sibling modules. That
    silently re-grows the monolith and shadows the canonical
    implementation. Either:

      - Move the new definition into the appropriate sibling module and
        re-export it from the dispatcher, OR
      - If the symbol genuinely belongs back in the dispatcher (rare),
        remove it from ``EXTRACTED_SYMBOLS`` in this test and also delete
        the sibling-module definition so there is only one source of truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        with open(DISPATCHER_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "pipeline_regression_dispatcher.py re-defines symbols that were "
            "extracted to sibling modules. This shadows the canonical "
            "implementation and re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical sibling "
            "module and let the dispatcher re-export it via "
            "`from app.<module> import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromDispatcher:
    """Tripwire — extracted symbols must remain importable via the facade.

    External callers (admin_digest, agent.py, task_completion,
    rollup_management, lifecycle, task_lifecycle, orchestrator, the
    pipeline-regression-dispatcher test file) historically imported these
    symbols from ``app.pipeline_regression_dispatcher`` and many still do.
    Removing the re-export import would silently break those call sites.
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_dispatcher(self, source_module: str, symbol: str):
        dispatcher = importlib.import_module("app.pipeline_regression_dispatcher")
        canonical = importlib.import_module(source_module)

        assert hasattr(dispatcher, symbol), (
            f"app.pipeline_regression_dispatcher.{symbol} is no longer "
            f"importable. Add the re-export:\n"
            f"    from {source_module} import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home for "
            f"this extracted symbol no longer defines it. Either restore the "
            f"definition there, or remove it from EXTRACTED_SYMBOLS in this "
            f"test if the extraction was reverted intentionally."
        )

        dispatcher_obj = getattr(dispatcher, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert dispatcher_obj is canonical_obj, (
            f"app.pipeline_regression_dispatcher.{symbol} is not the same "
            f"object as {source_module}.{symbol}. The dispatcher is "
            f"shadowing the canonical implementation — drop the local "
            f"definition and rely on the re-export."
        )
