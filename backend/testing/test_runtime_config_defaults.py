"""
Tests that registered runtime-config defaults match every read-site fallback.

A mismatch means unconfigured deployments silently get the wrong value — the
registered default should govern, not an arbitrary fallback literal.
"""

import ast
from collections import Counter
import pathlib
from typing import ClassVar

import pytest


BACKEND_APP = pathlib.Path(__file__).parent.parent / "app"

# Simple types that AST can represent and we can compare directly.
_SIMPLE_TYPES = (bool, int, float, str, type(None))


def _collect_get_fallbacks(filepath: pathlib.Path, key: str) -> list[tuple]:
    """Return list of (lineno, fallback_value) for .get("<key>", <default>) calls in source."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "get":
            continue
        if len(node.args) < 2:
            continue
        first_arg = node.args[0]
        if not (isinstance(first_arg, ast.Constant) and first_arg.value == key):
            continue
        second_arg = node.args[1]
        if isinstance(second_arg, ast.Constant):
            results.append((node.lineno, second_arg.value))
    return results


def _all_get_fallbacks_in_file(filepath: pathlib.Path) -> dict[str, list[tuple]]:
    """Return dict mapping key -> [(lineno, fallback)] for all .get('key', fallback) calls."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    results: dict[str, list[tuple]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "get":
            continue
        if len(node.args) < 2:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
            continue
        second_arg = node.args[1]
        if not isinstance(second_arg, ast.Constant):
            continue
        key = first_arg.value
        results.setdefault(key, []).append((node.lineno, second_arg.value))
    return results


def _all_py_files():
    return list(BACKEND_APP.rglob("*.py"))


class TestMaxActiveDirectionsFallbacks:
    """Every read-site fallback for max_active_directions must equal the registered default (10)."""

    def test_registered_default_is_10(self):
        from app.runtime_config_state import runtime_config as _defaults

        assert _defaults.get("max_active_directions") == 10, (
            "Registered default for max_active_directions changed — update "
            "read-site fallbacks and this test."
        )

    def test_no_stale_fallback_5_in_source(self):
        """No production source file may use .get('max_active_directions', 5)."""
        offenders = []
        for pyfile in _all_py_files():
            if pyfile.name.startswith("test_"):
                continue
            for lineno, fallback in _collect_get_fallbacks(pyfile, "max_active_directions"):
                if fallback != 10:
                    offenders.append(f"{pyfile.relative_to(BACKEND_APP.parent)}:{lineno} fallback={fallback!r}")

        assert not offenders, (
            "Read-sites with wrong fallback for max_active_directions "
            "(expected 10, registered default):\n" + "\n".join(offenders)
        )

    @pytest.mark.parametrize(
        "relpath",
        [
            "app/routers/directions.py",
            "app/routers/admin_agent_ops.py",
            "app/strategic_completion.py",
            "app/directions_repo.py",
        ],
    )
    def test_specific_files_use_fallback_10(self, relpath):
        filepath = BACKEND_APP.parent / relpath
        if not filepath.exists():
            pytest.skip(f"{relpath} not found")
        fallbacks = _collect_get_fallbacks(filepath, "max_active_directions")
        assert fallbacks, f"No .get('max_active_directions', ...) calls found in {relpath}"
        wrong = [(ln, v) for ln, v in fallbacks if v != 10]
        assert not wrong, (
            f"Stale fallback in {relpath}: "
            + ", ".join(f"line {ln} value={v!r}" for ln, v in wrong)
        )


class TestAllRegisteredDefaultsMatchReadSiteFallbacks:
    """For every registered key with a simple type, verify no read-site passes
    a hardcoded fallback that silently diverges from the registered default.

    This catches config-drift at CI time: if the registered default changes but
    a read-site's hardcoded fallback is not updated, this test fails.
    """

    # Allowlist of pre-existing divergences — known stale fallbacks that were
    # present before this test was written.  New divergences (not in this set)
    # will cause the test to fail.  Each tuple is (relpath, lineno, key).
    # Generated 2026-07-21; entries here document known staleness to be cleaned
    # up in follow-up PRs.  If a registered default changes, add NEW entries
    # here (do NOT remove the old ones without fixing the read-site).
    _KNOWN_STALE_FALLBACKS: ClassVar[set[tuple[str, int, str]]] = {
        ("app/anomaly_detection.py", 1170, "max_concurrent_vms"),
        ("app/autonomous_research.py", 1017, "evaluating_pipeline_timeout_minutes"),
        ("app/blocked_task_unblock_sweeps.py", 1499, "capability_proof_gate_mode"),
        ("app/blocked_task_unblock_sweeps.py", 1871, "capability_proof_gate_mode"),
        ("app/capability_execution_fence.py", 164, "capability_proof_gate_mode"),
        ("app/capability_proof.py", 1093, "capability_proof_execution_trials_enabled"),
        ("app/capability_proof.py", 1158, "capability_proof_execution_trials_enabled"),
        ("app/capability_proof.py", 2226, "capability_proof_execution_trials_enabled"),
        ("app/compute_experiment_contract.py", 563, "compute_experiment_spend_threshold_usd"),
        ("app/deploy_health.py", 467, "max_concurrent_vms"),
        ("app/evergreen_backlog.py", 947, "max_global_autonomous_tasks_per_hour"),
        ("app/main.py", 1311, "max_concurrent_vms"),
        ("app/metrics_integrity_sweep.py", 4842, "capability_proof_gate_mode"),
        ("app/orchestrator.py", 3836, "warm_backlog_reuse_enabled"),
        ("app/plan_completion.py", 1392, "capability_proof_gate_mode"),
        ("app/qualification_state_resolver.py", 486, "env_preflight_enabled"),
        ("app/release_promotion.py", 331, "env_preflight_enabled"),
        ("app/release_qualifier.py", 2917, "env_preflight_enabled"),
        ("app/release_qualifier.py", 3177, "capability_proof_gate_mode"),
        ("app/release_qualifier.py", 5388, "env_preflight_enabled"),
        ("app/release_qualifier.py", 7611, "capability_proof_gate_mode"),
        ("app/release_qualifier.py", 8238, "env_preflight_enabled"),
        ("app/research_completion.py", 6300, "capability_proof_gate_mode"),
        ("app/research_completion.py", 6334, "capability_proof_gate_mode"),
        ("app/routers/admin_health.py", 585, "warm_continuity_enabled"),
        ("app/routers/admin_health.py", 613, "warm_backlog_reuse_enabled"),
        ("app/routers/admin_health.py", 1167, "warm_continuity_enabled"),
        ("app/routers/admin_health.py", 1170, "warm_backlog_reuse_enabled"),
        ("app/routers/admin_settings.py", 520, "max_global_autonomous_tasks_per_hour"),
        ("app/routers/admin_settings.py", 556, "structural_health_audit_mode"),
        ("app/routers/admin_settings.py", 568, "capability_proof_gate_mode"),
        ("app/routers/admin_settings.py", 586, "capability_proof_execution_trials_enabled"),
        ("app/routers/admin_settings.py", 592, "metrics_integrity_sweep_max_checks"),
        ("app/routers/admin_settings.py", 604, "completion_debt_weight_mode"),
        ("app/routers/admin_settings.py", 693, "max_parallel_research_per_project"),
        ("app/routers/admin_settings.py", 746, "smoketest_enabled"),
        ("app/routers/admin_settings.py", 756, "warm_continuity_enabled"),
        ("app/routers/admin_settings.py", 759, "warm_backlog_reuse_enabled"),
        ("app/routers/admin_settings.py", 3394, "max_parallel_research_per_project"),
        ("app/routers/admin_settings.py", 3448, "warm_continuity_enabled"),
        ("app/routers/admin_settings.py", 3451, "warm_backlog_reuse_enabled"),
        ("app/routers/admin_settings.py", 3623, "smoketest_enabled"),
        ("app/routers/admin_settings.py", 3681, "completion_debt_weight_mode"),
        ("app/routers/admin_settings.py", 3686, "capability_proof_gate_mode"),
        ("app/routers/admin_settings.py", 3708, "capability_proof_execution_trials_enabled"),
        ("app/routers/admin_warm_pool.py", 100, "max_concurrent_vms"),
        ("app/routers/agent.py", 126, "repo_map_holdback_fraction"),
        ("app/routers/agent.py", 142, "module_interfaces_holdback_fraction"),
        ("app/routers/agent.py", 159, "related_tests_holdback_fraction"),
        ("app/routers/agent.py", 176, "affected_consumers_holdback_fraction"),
        ("app/routers/agent_status.py", 2494, "warm_continuity_enabled"),
        ("app/routers/agent_status.py", 2908, "warm_backlog_reuse_enabled"),
        ("app/routers/agent_warm_continuity.py", 1250, "warm_backlog_reuse_enabled"),
        ("app/services.py", 292, "max_retained_images"),
        ("app/services.py", 487, "auto_archive_max_terminal_tasks_per_project"),
        ("app/services.py", 636, "auto_archive_max_terminal_tasks_per_project"),
        ("app/strategic_spawning.py", 7408, "max_concurrent_vms"),
        ("app/task_completion.py", 3700, "capability_proof_execution_trials_enabled"),
        ("app/warm_pool.py", 172, "warm_pool_target_size"),
        ("app/warm_pool_maintainer.py", 137, "warm_pool_target_size"),
        ("app/warm_pool_maintainer.py", 1136, "max_concurrent_vms"),
    }

    # Line numbers are deliberately excluded: deletions elsewhere in a module
    # must not turn known fallback debt into a false-positive. The literal
    # fallback and occurrence count remain pinned so new or changed debt fails.
    _KNOWN_STALE_FALLBACK_COUNTS: ClassVar[Counter] = Counter({
        ("app/anomaly_detection.py", "max_concurrent_vms", 0): 1,
        ("app/autonomous_research.py", "evaluating_pipeline_timeout_minutes", 240): 1,
        ("app/blocked_task_unblock_sweeps.py", "capability_proof_gate_mode", "off"): 2,
        ("app/capability_execution_fence.py", "capability_proof_gate_mode", "off"): 1,
        ("app/capability_proof.py", "capability_proof_execution_trials_enabled", False): 3,
        ("app/compute_experiment_contract.py", "compute_experiment_spend_threshold_usd", 0.0): 1,
        ("app/deploy_health.py", "max_concurrent_vms", 0): 1,
        ("app/evergreen_backlog.py", "max_global_autonomous_tasks_per_hour", 500): 1,
        ("app/main.py", "max_concurrent_vms", 0): 1,
        ("app/metrics_integrity_sweep.py", "capability_proof_gate_mode", "off"): 1,
        ("app/orchestrator.py", "warm_backlog_reuse_enabled", False): 1,
        ("app/plan_completion.py", "capability_proof_gate_mode", "off"): 1,
        ("app/qualification_state_resolver.py", "smoketest_enabled", True): 1,
        ("app/release_promotion.py", "smoketest_enabled", True): 1,
        ("app/release_qualifier.py", "capability_proof_gate_mode", "off"): 2,
        ("app/research_completion.py", "capability_proof_gate_mode", "off"): 2,
        ("app/routers/admin_health.py", "warm_backlog_reuse_enabled", False): 2,
        ("app/routers/admin_health.py", "warm_continuity_enabled", False): 2,
        ("app/routers/admin_settings.py", "capability_proof_execution_trials_enabled", False): 2,
        ("app/routers/admin_settings.py", "capability_proof_gate_mode", "off"): 2,
        ("app/routers/admin_settings.py", "completion_debt_weight_mode", "observe"): 2,
        # env_preflight_enabled ships True in production (registry source default
        # AND promotion_policy.yaml `env_preflight_enabled: true`, seeded into
        # runtime_config at import). These two admin-settings GET read-sites keep a
        # True fallback that MATCHES that real production default. The autouse
        # `reset_runtime_config` fixture (testing/conftest.py) force-sets the key to
        # False for test isolation, so the guard's live `expected` reads False and
        # flags the correct-for-prod True fallback as debt. Pinned, NOT "fixed":
        # flipping these read-sites to False would misreport the prod default.
        ("app/routers/admin_settings.py", "env_preflight_enabled", True): 2,
        ("app/routers/admin_settings.py", "max_global_autonomous_tasks_per_hour", 500): 1,
        ("app/routers/admin_settings.py", "metrics_integrity_sweep_max_checks", 30): 1,
        ("app/routers/admin_settings.py", "structural_health_audit_mode", "off"): 1,
        ("app/routers/admin_settings.py", "warm_backlog_reuse_enabled", False): 2,
        ("app/routers/admin_settings.py", "warm_continuity_enabled", False): 2,
        ("app/routers/admin_warm_pool.py", "max_concurrent_vms", 0): 1,
        ("app/routers/agent.py", "affected_consumers_holdback_fraction", 0.0): 1,
        ("app/routers/agent.py", "module_interfaces_holdback_fraction", 0.0): 1,
        ("app/routers/agent.py", "related_tests_holdback_fraction", 0.0): 1,
        ("app/routers/agent.py", "repo_map_holdback_fraction", 0.0): 1,
        ("app/routers/agent_status.py", "warm_backlog_reuse_enabled", False): 1,
        ("app/routers/agent_status.py", "warm_continuity_enabled", False): 1,
        ("app/routers/agent_warm_continuity.py", "warm_backlog_reuse_enabled", False): 1,
        ("app/services.py", "auto_archive_max_terminal_tasks_per_project", 0): 2,
        ("app/services.py", "max_retained_images", 10): 1,
        ("app/strategic_spawning.py", "max_concurrent_vms", 0): 1,
        ("app/task_completion.py", "capability_proof_execution_trials_enabled", False): 1,
        ("app/warm_pool.py", "warm_pool_target_size", 0): 1,
        ("app/warm_pool_maintainer.py", "max_concurrent_vms", 0): 1,
        ("app/warm_pool_maintainer.py", "warm_pool_target_size", 0): 1,
    })

    def test_all_registered_key_fallbacks_match(self):
        """Scan every registered key across all read-sites and assert fallbacks match."""
        import app.runtime_config_state as _rcs

        registered = dict(_rcs.runtime_config)
        simple_defaults = {
            k: v for k, v in registered.items()
            if isinstance(v, _SIMPLE_TYPES)
        }

        # Collect all .get(key, fallback) across all non-test source files.
        # key -> [(relpath, lineno, fallback)]
        site_fallbacks: dict[str, list[tuple[str, int, object]]] = {}
        for pyfile in _all_py_files():
            if pyfile.name.startswith("test_"):
                continue
            rel = pyfile.relative_to(BACKEND_APP.parent).as_posix()
            # Skip runtime_config_state.py: its internal reads already use
            # config_get() (registry-sourced), and the .get() calls in
            # sync_runtime_config_from_state are validation-specific, not
            # read-site fallbacks.
            if rel == "app/runtime_config_state.py":
                continue
            for key, entries in _all_get_fallbacks_in_file(pyfile).items():
                if key not in simple_defaults:
                    continue
                for lineno, fallback in entries:
                    site_fallbacks.setdefault(key, []).append(
                        (rel, lineno, fallback)
                    )

        # Assert every read-site fallback matches the registered default,
        # excluding known pre-existing staleness.
        observed_stale: Counter = Counter()
        for key in sorted(site_fallbacks):
            expected = simple_defaults[key]
            for relpath, lineno, actual in site_fallbacks[key]:
                if actual == expected:
                    continue
                observed_stale[(relpath, key, actual)] += 1

        unexpected = observed_stale - self._KNOWN_STALE_FALLBACK_COUNTS
        resolved_without_baseline_update = (
            self._KNOWN_STALE_FALLBACK_COUNTS - observed_stale
        )
        assert not unexpected and not resolved_without_baseline_update, (
            "Runtime-config fallback debt changed. New or increased mismatches: "
            f"{dict(unexpected)}; resolved/decreased entries that should be "
            "removed from the pinned baseline: "
            f"{dict(resolved_without_baseline_update)}"
        )


def test_dormant_release_declarations_have_owner_reason_and_rearm_condition():
    """Every disabled release path is an owned, observable rollback declaration."""
    from app.runtime_config_state import (
        _DORMANT_OFF_FLAGS,
        iter_dormant_knob_debts,
        runtime_config,
    )

    assert _DORMANT_OFF_FLAGS, "disabled release paths must not be silently unregistered"
    missing = [
        key
        for key, declaration in _DORMANT_OFF_FLAGS.items()
        if not isinstance(declaration, dict)
        or not str(declaration.get("owner", "")).strip()
        or not str(declaration.get("reason", "")).strip()
        or not str(declaration.get("rearm_condition", "")).strip()
    ]
    assert not missing, (
        "Dormant release declarations require non-empty owner, reason, and "
        f"rearm_condition: {missing}"
    )
    debts = {
        item["detail"]["knob"]: item for item in iter_dormant_knob_debts()
    }
    for key, declaration in _DORMANT_OFF_FLAGS.items():
        if key == "mode_knob_graduation_enabled":
            assert runtime_config[key] is True, "the graduation harness must be armed"
            continue
        assert key in debts, f"inactive release declaration {key} was not ledgered"
        assert debts[key]["detail"]["owner"] == declaration["owner"]
        assert debts[key]["detail"]["rearm_condition"] == declaration["rearm_condition"]


def test_unarmed_promotion_required_dimension_is_declared():
    """A merge-grade blocker that cannot fire must have a registry disposition."""
    from app.runtime_config_state import _DORMANT_OFF_FLAGS, _read_promotion_policy_file

    policy = _read_promotion_policy_file() or {}
    threshold = float(policy.get("merge_grade_block_threshold", 0.0) or 0.0)
    if threshold <= 0.0:
        assert "merge_grade_block_threshold" in _DORMANT_OFF_FLAGS, (
            "merge_grade is promotion-required, but its blocker is unarmed and "
            "has no dormant-off declaration"
        )
