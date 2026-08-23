"""Regression tests for the derived test-estate queue."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
_GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_test_estate_ranked_queue",
    REPOSITORY_ROOT / "scripts" / "generate_test_estate_ranked_queue.py",
)
assert _GENERATOR_SPEC and _GENERATOR_SPEC.loader
_GENERATOR = importlib.util.module_from_spec(_GENERATOR_SPEC)
sys.modules[_GENERATOR_SPEC.name] = _GENERATOR
_GENERATOR_SPEC.loader.exec_module(_GENERATOR)
collect_test_files = _GENERATOR.collect_test_files
main = _GENERATOR.main
render_queue = _GENERATOR.render_queue


def _write_fixture_root(root: Path) -> None:
    (root / "backend" / "testing").mkdir(parents=True)
    (root / "admin-ui" / "src").mkdir(parents=True)
    (root / "agent" / "src").mkdir(parents=True)
    (root / "backend" / ".test_durations").write_text("{}", encoding="utf-8")
    for suite in ("admin-ui", "agent"):
        (root / suite / ".test_durations.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": "src/example.test.ts",
                            "duration_ms": 125,
                            "measured": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    (root / "admin-ui" / "src" / "example.test.ts").write_text(
        'it("renders", () => expect(true).toBe(true));\n', encoding="utf-8"
    )
    (root / "agent" / "src" / "example.test.ts").write_text(
        'test("runs", () => expect(true).toBe(true));\n', encoding="utf-8"
    )
    (root / "backend" / "testing" / "test_example.py").write_text(
        "def test_example():\n    assert 1 == 1\n", encoding="utf-8"
    )
    (root / "backend" / "testing" / "conversational_workday_manifest.json").write_text(
        json.dumps({"protect": []}), encoding="utf-8"
    )


def test_checked_in_queue_is_generated_from_current_tree() -> None:
    assert render_queue(REPOSITORY_ROOT) == (
        REPOSITORY_ROOT / "test-estate-ranked-queue.md"
    ).read_text(encoding="utf-8")


def test_generator_uses_canonical_counts_and_preserves_manifest_floor(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    backend_file = tmp_path / "backend" / "testing" / "test_example.py"
    backend_file.write_text(
        "import unittest\n\n"
        "class Helper(unittest.TestCase):\n"
        "    def test_inherited_collector_shape(self):\n"
        "        assert True\n\n"
        "def test_function():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    manifest = {
        "protect": [
            {
                "path": "backend/testing/test_example.py",
                "test_ids": ["test_function"],
                "test_count": 9,
            }
        ]
    }
    manifest_path = tmp_path / "backend/testing/conversational_workday_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()

    records = collect_test_files(tmp_path)
    backend_record = next(record for record in records if record.area == "backend")
    assert backend_record.test_count == 2
    assert backend_record.protected_declared == 9
    assert backend_record.cost_seconds is None
    output = render_queue(tmp_path)
    assert "protected; declared floor 9" in output
    assert "unknown (no committed measurement)" in output
    assert manifest_path.read_bytes() == before


def test_check_reports_threshold_crossing_until_queue_is_regenerated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_fixture_root(tmp_path)
    assert main(["--write", "--root", str(tmp_path)]) == 0
    assert main(["--check", "--root", str(tmp_path)]) == 0

    fixture = tmp_path / "backend" / "testing" / "test_example.py"
    fixture.write_text("def test_example():\n    assert 1 == 1\n" + "# growth\n" * 1500)
    assert main(["--check", "--root", str(tmp_path)]) == 1
    error = capsys.readouterr().err
    assert "Test-estate queue is stale" in error
    assert "moderate (1500\u20132999 LOC)" in error

    assert main(["--write", "--root", str(tmp_path)]) == 0
    assert main(["--check", "--root", str(tmp_path)]) == 0
    assert "moderate (1500\u20132999 LOC)" in (
        tmp_path / "test-estate-ranked-queue.md"
    ).read_text(encoding="utf-8")


def test_measured_frontend_cost_is_directional_signal(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    output = render_queue(tmp_path)
    assert "admin-ui/src/example.test.ts" in output
    assert "0.125s measured" in output
    assert output.index("0.125s measured") < output.index(
        "backend/testing/test_example.py"
    )
