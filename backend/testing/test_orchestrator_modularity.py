"""Modularity tripwire for ``app.orchestrator``.

The orchestrator monolith is being broken up along clean responsibility
seams — the cycle / health / dependency classification helpers now live
in ``app.orchestrator_health`` and the dispatch-list shaping passes
(health-aware reordering, pipeline slot-cap, reserved-type promotion) now
live in ``app.orchestrator_health`` / ``app.orchestrator_filters``. The
orchestrator re-exports each extracted symbol so existing
``from app.orchestrator import X`` callers (tests, reaper, llm-runtime
tests, background tests, pipeline-reliability tests) keep working.

This tripwire enforces two halves of the facade contract for each
EXTRACTED_SYMBOLS entry:

  1. The symbol is NOT defined at module top-level inside
     ``orchestrator.py`` (no ``def`` / ``async def`` / ``class`` /
     top-level ``Name = ...`` assignment). Re-export imports are
     allowed and expected.
  2. The symbol IS still importable as
     ``from app.orchestrator import X``.

Future extractions should extend EXTRACTED_SYMBOLS with the names they
moved so the seam stays guarded automatically.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


ORCHESTRATOR_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "orchestrator.py")
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.orchestrator_health": (
        "_CRITICAL_EXTERNAL_DEPS",
        "_check_candidate_task_deps",
        "_classify_stuck_tasks",
        "_find_cycle_members",
        "_health_score_task",
        "get_confirmed_down_critical_deps",
        # Dispatch-ordering seam: health-aware reordering pass.
        "reorder_eligible_by_health",
    ),
    "app.orchestrator_filters": (
        # Dispatch-ordering seam: pipeline slot-cap + reserved-type promotion.
        "cap_pipeline_slots",
        "reserve_slots_for_types",
    ),
    "app.orchestrator_provision": (
        # VM provisioning + provision-failure handling (the create_task target
        # the dispatch loop fires once a task is committed to PROVISIONING).
        "provision_and_handle",
    ),
    "app.orchestrator_runner_dispatch": (
        # Local-runner + compute-experiment dispatch cluster.
        "RunnerDispatchSnapshot",
        "_runner_handles_task_type",
        "_runner_at_capacity",
        "_load_active_runners_cache",
        "_try_runner_dispatch",
        "_task_requires_strategic_local_runner",
        "_hold_strategic_local_runner_task",
        "_try_strategic_local_required_runner_path",
        "_num_or_none",
        "_select_eligible_runner",
        "_build_compute_launch_kwargs",
        "_fail_compute_experiment_task",
        "_dispatch_compute_experiment_directly",
        "_deactivate_stale_runners",
        "_fail_stale_local_runner_holds",
        "_emit_all_accounts_blocked_stall",
        "_sweep_runner_deactivated_escalations",
        "_RUNNER_LIVENESS_SECONDS",
        "_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS",
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


class TestExtractedSymbolsNotRedefinedInOrchestrator:
    """Tripwire — an extracted symbol must not be re-defined in orchestrator.

    If this test fails, a recent change inside
    ``backend/app/orchestrator.py`` added a top-level ``def`` /
    ``class`` / module-level assignment whose name collides with a
    symbol that already lives in one of the sibling modules. That
    silently re-grows the monolith and shadows the canonical
    implementation. Either:

      - Move the new definition into the appropriate sibling module
        (``app.orchestrator_health`` etc.) and re-export it from the
        orchestrator, OR
      - If the symbol genuinely belongs back in the orchestrator (rare),
        remove it from ``EXTRACTED_SYMBOLS`` in this test and also delete
        the sibling-module definition so there is only one source of
        truth.
    """

    def test_no_extracted_symbol_is_redefined(self):
        with open(ORCHESTRATOR_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "orchestrator.py re-defines symbols that were extracted to "
            "sibling modules. This shadows the canonical implementation "
            "and re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical sibling "
            "module and let the orchestrator re-export it via "
            "`from app.<module> import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromOrchestrator:
    """Tripwire — extracted symbols must remain importable via the facade.

    External callers (reaper, llm-runtime tests, background tests,
    pipeline-reliability tests, orchestrator tests) historically
    imported these symbols from ``app.orchestrator`` and many still
    do. Removing the re-export import would silently break those call
    sites.
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_orchestrator(self, source_module: str, symbol: str):
        orchestrator = importlib.import_module("app.orchestrator")
        canonical = importlib.import_module(source_module)

        assert hasattr(orchestrator, symbol), (
            f"app.orchestrator.{symbol} is no longer importable. "
            f"Add the re-export:\n"
            f"    from {source_module} import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home "
            f"for this extracted symbol no longer defines it. Either "
            f"restore the definition there, or remove it from "
            f"EXTRACTED_SYMBOLS in this test if the extraction was "
            f"reverted intentionally."
        )

        orchestrator_obj = getattr(orchestrator, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert orchestrator_obj is canonical_obj, (
            f"app.orchestrator.{symbol} is not the same object as "
            f"{source_module}.{symbol}. The orchestrator is shadowing "
            f"the canonical implementation — drop the local definition "
            f"and rely on the re-export."
        )


class TestOrchestratorHealthModuleExists:
    """The extracted sibling module must exist and be importable."""

    def test_module_importable(self):
        mod = importlib.import_module("app.orchestrator_health")
        assert mod is not None


class TestOrchestratorProvisionModuleExists:
    """The extracted provisioning module must exist and be importable.

    Also guards the re-export cycle: importing the leaf module first must not
    deadlock (``provision_and_handle`` imports ``app.orchestrator`` lazily, so a
    leaf-first import is safe) and must expose the same object the facade does.
    """

    def test_module_importable(self):
        mod = importlib.import_module("app.orchestrator_provision")
        assert mod is not None
        assert hasattr(mod, "provision_and_handle")

    def test_facade_reexports_same_object(self):
        prov = importlib.import_module("app.orchestrator_provision")
        orch = importlib.import_module("app.orchestrator")
        assert orch.provision_and_handle is prov.provision_and_handle
