"""
Tests that registered runtime-config defaults match every read-site fallback.

A mismatch means unconfigured deployments silently get the wrong value — the
registered default should govern, not an arbitrary fallback literal.
"""

import ast
import pathlib
import pytest


BACKEND_APP = pathlib.Path(__file__).parent.parent / "app"


def _collect_get_fallbacks(filepath: pathlib.Path, key: str) -> list[tuple]:
    """Return list of (lineno, fallback_value) for .get("<key>", <default>) calls in source."""
    source = filepath.read_text()
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


class TestBlindMultiReviewerGateFallback:
    """blind_multi_reviewer_gate_enabled read-site must match the registered default (True)."""

    def test_registered_default_is_true(self):
        from app.runtime_config_state import runtime_config as _defaults

        assert _defaults.get("blind_multi_reviewer_gate_enabled") is True, (
            "Registered default for blind_multi_reviewer_gate_enabled changed — update "
            "read-site fallbacks and this test."
        )

    def test_no_stale_false_fallback_in_source(self):
        """No production source file may use .get('blind_multi_reviewer_gate_enabled', False)."""
        offenders = []
        for pyfile in _all_py_files():
            if pyfile.name.startswith("test_"):
                continue
            for lineno, fallback in _collect_get_fallbacks(
                pyfile, "blind_multi_reviewer_gate_enabled"
            ):
                if fallback is not True:
                    offenders.append(
                        f"{pyfile.relative_to(BACKEND_APP.parent)}:{lineno} fallback={fallback!r}"
                    )

        assert not offenders, (
            "Read-sites with wrong fallback for blind_multi_reviewer_gate_enabled "
            "(expected True, registered default):\n" + "\n".join(offenders)
        )

    def test_task_lifecycle_fallback_is_true(self):
        filepath = BACKEND_APP / "task_lifecycle.py"
        if not filepath.exists():
            pytest.skip("task_lifecycle.py not found")
        fallbacks = _collect_get_fallbacks(filepath, "blind_multi_reviewer_gate_enabled")
        assert fallbacks, "No .get('blind_multi_reviewer_gate_enabled', ...) found in task_lifecycle.py"
        wrong = [(ln, v) for ln, v in fallbacks if v is not True]
        assert not wrong, (
            "Stale fallback in task_lifecycle.py: "
            + ", ".join(f"line {ln} value={v!r}" for ln, v in wrong)
        )
