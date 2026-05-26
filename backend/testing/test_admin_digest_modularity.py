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

Additional sub-router mount tripwires (``TestViewportDebtSubRouterMounted``,
``TestSrDiagnosticsSubRouterMounted``, ``TestAnomalyDetectionSubRouterMounted``,
``TestEfficiencySnapshotSubRouterMounted``, ``TestPipelineObsEndpointsExtracted``)
check that sibling sub-routers remain mounted on ``admin_digest.router``.
The checks are pure module introspection on the router objects; no
FastAPI app or TestClient is constructed.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest
from fastapi.routing import APIRoute


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
        "compute_anomaly_adjustment_summary",
        "compute_digest_trend_history",
        "compute_direction_progress",
        "compute_failure_grouping",
        "compute_migration_health",
        "compute_rejection_pattern_summary",
        "compute_resurrection_outcome_summary",
        "compute_retry_pattern_summary",
        "compute_review_cap_exhaustion_summary",
    ),
    "app.routers.admin_pipeline_observability": (
        "get_canonical_regression_layers",
        "get_drift_snapshot",
        "get_fix_loop_convergence",
        "get_fix_loop_convergence_trend",
        "get_fix_loop_diverged_tasks",
        "get_pipeline_efficiency",
        "get_pipeline_regressions",
        "get_provider_cost_summary",
        "get_regression_dispatcher_health",
        "get_regression_escalation_outcomes",
        "get_regression_mtta",
        "get_warm_reuse_funnel",
    ),
    "app.anomaly_detection": ("get_anomaly_tracker_snapshot",),
    "app.efficiency_snapshot": (
        "get_efficiency_axes",
        "get_llm_cost_breakdown",
    ),
    "app.regression_case_endpoints": ("clear_regression_manual_intervention",),
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
    break those call sites (including ``unittest.mock.patch`` targets
    that monkey-patch the admin_digest namespace). PR #5877 was the
    precedent that demonstrates this failure mode at scale — this test
    exists to keep it from recurring.
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
            f"rely on the re-export "
            f"(`from {source_module} import {symbol}  # noqa: F401`)."
        )


class TestSrDiagnosticsSubRouterMounted:
    """Tripwire — the ``sr_diagnostics_endpoints`` sub-router is mounted on admin_digest.

    The SR diagnostics endpoints live in their own module
    (``app.sr_diagnostics_endpoints``) but are exposed under the
    ``/api/admin`` prefix by mounting ``sr_diagnostics_endpoints.router`` onto
    ``admin_digest.router``.  Removing that line would silently drop all
    SR retrospective, blind-spot, and investigation endpoints from the API.
    """

    def test_sr_diagnostics_router_is_mounted_on_admin_digest(self):
        admin_digest = importlib.import_module("app.routers.admin_digest")
        sr_diagnostics = importlib.import_module("app.sr_diagnostics_endpoints")

        api_routes = [r for r in admin_digest.router.routes if isinstance(r, APIRoute)]
        assert api_routes, "app.routers.admin_digest.router has no APIRoute entries."

        endpoint_modules = {r.endpoint.__module__ for r in api_routes}
        assert "app.sr_diagnostics_endpoints" in endpoint_modules, (
            "app.routers.admin_digest.router does not include any routes "
            "whose endpoint comes from app.sr_diagnostics_endpoints. The "
            "sr_diagnostics_endpoints sub-router is no longer mounted. "
            "Restore the include:\n"
            "    from app.sr_diagnostics_endpoints import router as "
            "_sr_diagnostics_router  # noqa: E402\n"
            "    router.include_router(_sr_diagnostics_router)\n"
            f"Routes currently come from modules: {sorted(endpoint_modules)}"
        )

        sr_paths = [
            r.path for r in sr_diagnostics.router.routes if isinstance(r, APIRoute)
        ]
        admin_digest_paths = [r.path for r in api_routes]
        missing = [
            p
            for p in sr_paths
            if not any(adp.endswith(p) for adp in admin_digest_paths)
        ]
        assert not missing, (
            "sr_diagnostics_endpoints routes are not all mounted on admin_digest.\n"
            f"  Missing paths: {sorted(missing)}\n"
            "Ensure `router.include_router(_sr_diagnostics_router)` is "
            "present in app/routers/admin_digest.py."
        )


class TestAnomalyDetectionSubRouterMounted:
    """Tripwire — the ``anomaly_detection`` sub-router is mounted on admin_digest.

    The anomaly-tracker snapshot endpoint lives in ``app.anomaly_detection`` but
    is exposed under the ``/api/admin`` prefix by mounting
    ``anomaly_detection.router`` onto ``admin_digest.router``. Removing that line
    would silently drop the ``/api/admin/anomaly-tracker/snapshot`` endpoint.
    """

    def test_anomaly_detection_router_is_mounted_on_admin_digest(self):
        admin_digest = importlib.import_module("app.routers.admin_digest")
        anomaly_detection = importlib.import_module("app.anomaly_detection")

        api_routes = [r for r in admin_digest.router.routes if isinstance(r, APIRoute)]
        assert api_routes, "app.routers.admin_digest.router has no APIRoute entries."

        endpoint_modules = {r.endpoint.__module__ for r in api_routes}
        assert "app.anomaly_detection" in endpoint_modules, (
            "app.routers.admin_digest.router does not include any routes "
            "whose endpoint comes from app.anomaly_detection. The "
            "anomaly_detection sub-router is no longer mounted. "
            "Restore the include:\n"
            "    from app.anomaly_detection import router as "
            "_anomaly_detection_router  # noqa: E402\n"
            "    router.include_router(_anomaly_detection_router)\n"
            f"Routes currently come from modules: {sorted(endpoint_modules)}"
        )

        anomaly_paths = [
            r.path for r in anomaly_detection.router.routes if isinstance(r, APIRoute)
        ]
        admin_digest_paths = [r.path for r in api_routes]
        missing = [
            p
            for p in anomaly_paths
            if not any(adp.endswith(p) for adp in admin_digest_paths)
        ]
        assert not missing, (
            "anomaly_detection routes are not all mounted on admin_digest.\n"
            f"  Missing paths: {sorted(missing)}\n"
            "Ensure `router.include_router(_anomaly_detection_router)` is "
            "present in app/routers/admin_digest.py."
        )


class TestEfficiencySnapshotSubRouterMounted:
    """Tripwire — the ``efficiency_snapshot`` sub-router is mounted on admin_digest.

    The efficiency-axes and LLM-cost-breakdown endpoints live in
    ``app.efficiency_snapshot`` but are exposed under the ``/api/admin``
    prefix by mounting ``efficiency_snapshot.router`` onto
    ``admin_digest.router``.  Removing that line would silently drop the
    ``/api/admin/projects/{project_id}/efficiency-axes`` and
    ``/api/admin/projects/{project_id}/llm-cost-breakdown`` endpoints.
    """

    def test_efficiency_snapshot_router_is_mounted_on_admin_digest(self):
        admin_digest = importlib.import_module("app.routers.admin_digest")
        efficiency_snapshot = importlib.import_module("app.efficiency_snapshot")

        api_routes = [r for r in admin_digest.router.routes if isinstance(r, APIRoute)]
        assert api_routes, "app.routers.admin_digest.router has no APIRoute entries."

        endpoint_modules = {r.endpoint.__module__ for r in api_routes}
        assert "app.efficiency_snapshot" in endpoint_modules, (
            "app.routers.admin_digest.router does not include any routes "
            "whose endpoint comes from app.efficiency_snapshot. The "
            "efficiency_snapshot sub-router is no longer mounted. "
            "Restore the include:\n"
            "    from app.efficiency_snapshot import router as "
            "_efficiency_snapshot_router  # noqa: E402\n"
            "    router.include_router(_efficiency_snapshot_router)\n"
            f"Routes currently come from modules: {sorted(endpoint_modules)}"
        )

        efficiency_paths = [
            r.path for r in efficiency_snapshot.router.routes if isinstance(r, APIRoute)
        ]
        admin_digest_paths = [r.path for r in api_routes]
        missing = [
            p
            for p in efficiency_paths
            if not any(adp.endswith(p) for adp in admin_digest_paths)
        ]
        assert not missing, (
            "efficiency_snapshot routes are not all mounted on admin_digest.\n"
            f"  Missing paths: {sorted(missing)}\n"
            "Ensure `router.include_router(_efficiency_snapshot_router)` is "
            "present in app/routers/admin_digest.py."
        )


class TestViewportDebtSubRouterMounted:
    """Tripwire — the ``viewport_debt`` sub-router is mounted on admin_digest.

    The viewport-debt analytics endpoints live in their own module
    (``app.viewport_debt``) but are intentionally exposed under the
    ``/api/admin`` prefix by mounting ``viewport_debt.router`` onto
    ``admin_digest.router`` via ``router.include_router(...)``. Removing
    that line would silently drop the
    ``/api/admin/projects/{project_id}/viewport-debt`` endpoint from the
    public API surface.

    This check is pure module introspection on the router objects (no
    FastAPI app, no TestClient), in the same spirit as the AST + importlib
    checks above.
    """

    def test_viewport_debt_router_is_mounted_on_admin_digest(self):
        admin_digest = importlib.import_module("app.routers.admin_digest")
        viewport_debt = importlib.import_module("app.viewport_debt")

        api_routes = [r for r in admin_digest.router.routes if isinstance(r, APIRoute)]
        assert api_routes, (
            "app.routers.admin_digest.router has no APIRoute entries — "
            "this almost certainly means the router failed to register "
            "any endpoints, including the viewport_debt sub-router."
        )

        endpoint_modules = {r.endpoint.__module__ for r in api_routes}
        assert "app.viewport_debt" in endpoint_modules, (
            "app.routers.admin_digest.router does not include any routes "
            "whose endpoint comes from app.viewport_debt. The viewport_debt "
            "sub-router is no longer mounted. Restore the include:\n"
            "    from app.viewport_debt import router as _viewport_debt_router"
            "  # noqa: E402\n"
            "    router.include_router(_viewport_debt_router)\n"
            f"Routes currently come from modules: {sorted(endpoint_modules)}"
        )

        # Stronger check: every concrete viewport_debt APIRoute path should
        # appear (possibly prefixed by the admin_digest router's own
        # ``/api/admin`` prefix) on admin_digest after the include_router
        # merge.
        viewport_paths = [
            r.path for r in viewport_debt.router.routes if isinstance(r, APIRoute)
        ]
        admin_digest_paths = [r.path for r in api_routes]
        missing = [
            vp
            for vp in viewport_paths
            if not any(adp.endswith(vp) for adp in admin_digest_paths)
        ]
        assert not missing, (
            "viewport_debt routes are not all mounted on admin_digest.\n"
            f"  Missing paths (no matching suffix in admin_digest): "
            f"{sorted(missing)}\n"
            "Ensure `router.include_router(_viewport_debt_router)` is "
            "present in app/routers/admin_digest.py."
        )


# ---------------------------------------------------------------------------
# Pipeline-observability sub-router tripwire
# ---------------------------------------------------------------------------

# Endpoints that landed in app.routers.admin_pipeline_observability for cluster
# 1/4. Three sibling endpoints from the original cluster (get_anomaly_tracker_snapshot,
# get_efficiency_axes, get_llm_cost_breakdown) live in their own sibling routers
# (anomaly_detection, efficiency_snapshot) and are enforced by EXTRACTED_SYMBOLS above.
_PIPELINE_OBS_ENDPOINTS = (
    "get_provider_cost_summary",
    "get_drift_snapshot",
    "get_fix_loop_convergence",
    "get_fix_loop_convergence_trend",
    "get_fix_loop_diverged_tasks",
    "get_pipeline_regressions",
    "get_regression_dispatcher_health",
    "get_regression_mtta",
    "get_warm_reuse_funnel",
    "get_regression_escalation_outcomes",
    "get_pipeline_efficiency",
    "get_canonical_regression_layers",
)


class TestPipelineObsEndpointsExtracted:
    """Tripwire — pipeline-observability endpoints must not live in admin_digest.

    Checks two halves of the extraction contract:
      1. None of the endpoint function names are defined at top-level in
         ``backend/app/routers/admin_digest.py`` (AST check).
      2. All function names are present in the canonical module
         ``app.routers.admin_pipeline_observability`` and the sub-router is
         mounted on ``admin_digest.router``.
    """

    def test_endpoints_absent_from_admin_digest(self):
        with open(ADMIN_DIGEST_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations = [
            f"  {name} ({defs[name]})"
            for name in _PIPELINE_OBS_ENDPOINTS
            if name in defs
        ]
        assert not violations, (
            "app/routers/admin_digest.py still defines pipeline-observability "
            "endpoint(s) that were supposed to be extracted to "
            "app/routers/admin_pipeline_observability.py:\n" + "\n".join(violations)
        )

    def test_endpoints_present_in_canonical_module(self):
        obs = importlib.import_module("app.routers.admin_pipeline_observability")
        missing = [name for name in _PIPELINE_OBS_ENDPOINTS if not hasattr(obs, name)]
        assert not missing, (
            "app.routers.admin_pipeline_observability is missing endpoint(s) "
            "that should have been extracted there:\n"
            + "\n".join(f"  {n}" for n in missing)
        )

    def test_pipeline_obs_router_mounted_on_admin_digest(self):
        admin_digest = importlib.import_module("app.routers.admin_digest")
        obs = importlib.import_module("app.routers.admin_pipeline_observability")

        endpoint_modules = {
            r.endpoint.__module__
            for r in admin_digest.router.routes
            if isinstance(r, APIRoute)
        }
        assert "app.routers.admin_pipeline_observability" in endpoint_modules, (
            "app.routers.admin_digest.router does not include any routes from "
            "app.routers.admin_pipeline_observability. Restore the include:\n"
            "    from app.routers.admin_pipeline_observability import router as "
            "_pipeline_obs_router  # noqa: E402\n"
            "    router.include_router(_pipeline_obs_router)\n"
            f"Routes currently come from modules: {sorted(endpoint_modules)}"
        )

        # Verify all extracted endpoint paths are accessible via admin_digest.
        obs_paths = [r.path for r in obs.router.routes if isinstance(r, APIRoute)]
        admin_paths = [
            r.path for r in admin_digest.router.routes if isinstance(r, APIRoute)
        ]
        missing_paths = [
            p for p in obs_paths if not any(adp.endswith(p) for adp in admin_paths)
        ]
        assert not missing_paths, (
            "pipeline_observability routes are not all reachable via admin_digest:\n"
            f"  Missing paths: {sorted(missing_paths)}"
        )
