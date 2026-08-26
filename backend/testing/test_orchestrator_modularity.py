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


# ── Runner-dispatch facade ───────────────────────────────────────────────────
# ``orchestrator_runner_dispatch.py`` is itself a chronic size-limit offender
# (6 recorded cap bumps). Cap bumps are forbidden by ``app.size_limit_policy``,
# so it was decomposed along the same facade contract the orchestrator uses:
# each cohesive seam moved to a sibling module and is re-exported so every
# existing ``from app.orchestrator_runner_dispatch import X`` call site (and the
# ``from app.orchestrator import X`` chain behind it) keeps working unchanged.
#
# The tripwire below is what stops the module from re-growing: an agent that
# adds a NEW top-level definition here under one of these names shadows the
# canonical implementation, and the two copies drift silently.
RUNNER_DISPATCH_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "app", "orchestrator_runner_dispatch.py"
    )
)


DISPATCH_EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.orchestrator_runner_placement": (
        "EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY",
        "_stage_local_only_placement_wait",
        "_clear_execution_placement_wait",
        "_hold_task_for_local_only_placement",
    ),
    "app.runner_capability_contracts": (
        "_coerce_vram_gb",
        "_coerce_min_compute_capability",
        "_container_image_min_free_disk_bytes",
        "_container_workspace_min_free_disk_bytes",
        "_paths_require_browser",
        "_payload_requires_browser",
        "_task_requires_browser",
        "_runner_lacks_required_browser",
        "_task_requires_project_verification_pinned",
        "_runner_lacks_required_project_verification",
        "_fleet_missing_capability_reason",
    ),
    "app.orchestrator_runner_selection": (
        "FORCED_RUNNER_TARGET_CONTEXT_KEY",
        "_num_or_none",
        "_payload_runner_target",
        "_PIN_HOLD_DIGITS_RE",
        "_pin_hold_stable_key",
        "_diagnose_strict_runner_pin_hold",
        "_lookup_runner_hostname",
        "_select_eligible_runner",
    ),
    "app.orchestrator_compute_experiment_dispatch": (
        "AWAITING_FORCED_RUNNER_CONTEXT_KEY",
        "COMPUTE_RUNNER_HOLD_CONTEXT_KEYS",
        "COMPUTE_LAUNCH_DEFERRED_REASON_CONTEXT_KEY",
        "COMPUTE_LAUNCH_DEFER_COUNT_CONTEXT_KEY",
        "_COMPUTE_CAP_DEFER_BASE_MINUTES",
        "_COMPUTE_CAP_DEFER_MAX_MINUTES",
        "_COMPUTE_CAP_DEFER_ALERT_AFTER_DEFERS",
        "_COMPUTE_CAP_DEFER_ALERT_TITLE",
        "_build_compute_launch_kwargs",
        "_fail_compute_experiment_task",
        "_defer_compute_experiment_task",
        "_maybe_alert_saturated_compute_cap_defer",
        "_dispatch_compute_experiment_directly",
    ),
}


def _flatten_dispatch_symbols() -> list[tuple[str, str]]:
    """Return (source_module, symbol_name) pairs from DISPATCH_EXTRACTED_SYMBOLS."""
    return [
        (module, name)
        for module, names in DISPATCH_EXTRACTED_SYMBOLS.items()
        for name in names
    ]


class TestRunnerDispatchExtractedSymbolsNotRedefined:
    """Tripwire — an extracted seam must not be re-defined in the dispatch module."""

    def test_no_extracted_symbol_is_redefined(self):
        with open(RUNNER_DISPATCH_PATH, encoding="utf-8") as fh:
            defs = _top_level_definitions(fh.read())

        violations = [
            f"  {name} ({defs[name]}) — canonical home: {module}"
            for module, name in _flatten_dispatch_symbols()
            if name in defs
        ]

        assert not violations, (
            "orchestrator_runner_dispatch.py re-defines symbols that were "
            "extracted to sibling modules. It is a chronic size-limit offender, "
            "so a shadowing copy both re-grows the module and silently drifts "
            "from the canonical implementation.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: keep the definition in its canonical sibling module and "
            "re-export it via `from app.<module> import <name>  # noqa: F401`."
        )


class TestRunnerDispatchExtractedSymbolsReExported:
    """Tripwire — every extracted seam stays importable from the dispatch facade.

    ``app.routers.runner``, ``app.blocked_retry_authority``,
    ``app.release_qualifier_audit`` and a large body of tests import these by
    name from ``app.orchestrator_runner_dispatch``; dropping a re-export breaks
    them at import time (or, worse, at first call inside a lazy import).
    """

    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_dispatch_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_reexported_and_identical(self, source_module: str, symbol: str):
        dispatch = importlib.import_module("app.orchestrator_runner_dispatch")
        canonical = importlib.import_module(source_module)

        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home for this "
            f"extracted symbol no longer defines it."
        )
        assert hasattr(dispatch, symbol), (
            f"app.orchestrator_runner_dispatch.{symbol} is no longer importable. "
            f"Add the re-export:\n"
            f"    from {source_module} import {symbol}  # noqa: F401"
        )
        assert getattr(dispatch, symbol) is getattr(canonical, symbol), (
            f"app.orchestrator_runner_dispatch.{symbol} is not the same object as "
            f"{source_module}.{symbol} — the facade is shadowing the canonical "
            f"implementation."
        )


class TestRunnerDispatchSiblingModulesImportLeafFirst:
    """Each extracted sibling must import standalone, before the facade.

    The facade imports all four siblings at module load, so a sibling that
    imported the facade back would deadlock only when the LEAF is imported
    first — exactly what ``app.routers.runner``'s lazy
    ``from app.orchestrator_runner_dispatch import ...`` does at request time.
    Importing each leaf in a fresh interpreter proves the dependency edges run
    one way.
    """

    # Each parametrisation spawns an interpreter that imports one backend
    # module; measured worst case 12.73s
    # (app.orchestrator_compute_experiment_dispatch) on an idle host against
    # the lanes' --timeout=30. 120s leaves ~9x headroom and stays above the
    # inner subprocess timeout so a genuinely hung import reports as itself.
    @pytest.mark.timeout(120)
    @pytest.mark.parametrize("module_name", sorted(DISPATCH_EXTRACTED_SYMBOLS))
    def test_leaf_module_imports_standalone(self, module_name: str):
        import subprocess
        import sys

        repo_backend = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            cwd=repo_backend,
            capture_output=True,
            text=True,
            # Half the marker above, so a genuinely hung import raises
            # TimeoutExpired here -- naming the module -- instead of being
            # killed by pytest-timeout with no attribution.
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"{module_name} could not be imported on its own — the extracted "
            f"sibling has an import cycle back to the dispatch facade.\n"
            f"{proc.stderr[-2000:]}"
        )
