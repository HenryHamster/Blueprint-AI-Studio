"""Standalone tests for backend/scripts/measure_repair_success.py.

No database required.  Run with:
    pytest testing/test_measure_repair_success.py -v --noconftest

Regression guard
----------------
A compute experiment previously failed with:
    ModuleNotFoundError: No module named 'backend.app.db'
because the experiment command imported a non-existent module.  The correct
module is ``backend.app.database``.  ``measure_repair_success.py`` was written
as the canonical replacement: it is directly lintable, directly testable, and
avoids any database import.  These tests ensure the script stays importable and
produces the correct metric key / value shape expected by the compute-experiment
success criterion.
"""
import importlib.util
import os
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "measure_repair_success.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("measure_repair_success", _SCRIPT_PATH)
    assert spec is not None, f"Could not locate script at {_SCRIPT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_script_is_importable_without_database():
    """The script must load clean — no ``backend.app.db`` or any other DB import."""
    mod = _load_module()
    assert callable(getattr(mod, "compute_success_rate", None))
    assert callable(getattr(mod, "main", None))


def test_compute_success_rate_all_pass():
    mod = _load_module()
    assert mod.compute_success_rate({"numPassedTests": 13, "numFailedTests": 0}) == 1.0


def test_compute_success_rate_partial():
    mod = _load_module()
    rate = mod.compute_success_rate({"numPassedTests": 7, "numFailedTests": 3})
    assert abs(rate - 0.7) < 0.001


def test_compute_success_rate_all_fail():
    mod = _load_module()
    assert mod.compute_success_rate({"numPassedTests": 0, "numFailedTests": 5}) == 0.0


def test_compute_success_rate_no_tests():
    mod = _load_module()
    assert mod.compute_success_rate({}) == 0.0


def test_compute_success_rate_missing_fields_treated_as_zero():
    mod = _load_module()
    assert mod.compute_success_rate({"numPassedTests": 4}) == 1.0


def test_output_contains_correct_metric_key(tmp_path):
    """Metric key must be ``overall_success_rate`` to match the experiment criterion."""
    import json

    mod = _load_module()
    rf = tmp_path / "result.json"
    os.environ["BLUEPRINT_RESULT_FILE"] = str(rf)
    original_run_vitest = mod.run_vitest
    try:
        mod.run_vitest = lambda _f, cwd: (0, {"numPassedTests": 10, "numFailedTests": 0})
        mod.main()
    finally:
        mod.run_vitest = original_run_vitest
        os.environ.pop("BLUEPRINT_RESULT_FILE", None)

    data = json.loads(rf.read_text())
    assert "overall_success_rate" in data, (
        "Metric key 'overall_success_rate' missing — experiment criterion would never fire"
    )
    assert 0.0 <= data["overall_success_rate"] <= 1.0


def test_output_written_when_result_file_set(tmp_path):
    import json

    mod = _load_module()
    rf = tmp_path / "result.json"
    os.environ["BLUEPRINT_RESULT_FILE"] = str(rf)
    original_run_vitest = mod.run_vitest
    try:
        mod.run_vitest = lambda _f, cwd: (0, {"numPassedTests": 5, "numFailedTests": 0})
        mod.main()
    finally:
        mod.run_vitest = original_run_vitest
        os.environ.pop("BLUEPRINT_RESULT_FILE", None)

    assert rf.exists()
    data = json.loads(rf.read_text())
    assert data["overall_success_rate"] == 1.0


def test_no_db_import_in_script():
    """Regression guard: the script must never import backend.app.db (non-existent module).

    Uses AST inspection so a mention in a docstring/comment does not false-fire.
    """
    import ast

    src = _SCRIPT_PATH.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("backend.app.db"), (
                    f"Script imports '{alias.name}' — 'backend.app.db' does not exist; "
                    "use 'backend.app.database'"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("backend.app.db"), (
                f"Script imports from '{module}' — 'backend.app.db' does not exist; "
                "use 'backend.app.database'"
            )
