"""Regression tests for the backend duration artifact generators."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import merge_durations
from testing.fixgate_attribution import format_offender_marker


REGENERATE_PATH = Path(__file__).with_name("regenerate-durations.py")


def _write_json(path: Path, data: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_test_file(root: Path) -> None:
    test_file = root / "testing" / "test_live.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "def test_untouched():\n    pass\n\n"
        "def test_remeasured():\n    pass\n\n"
        "def test_new():\n    pass\n",
        encoding="utf-8",
    )


def _load_regenerate_module():
    original_cwd = os.getcwd()
    spec = importlib.util.spec_from_file_location("regenerate_durations", REGENERATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        os.chdir(original_cwd)
    return module


def test_merge_preserves_baseline_and_fresh_partial_wins(tmp_path: Path) -> None:
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(
        output,
        {
            "testing/test_live.py::test_untouched": 1.25,
            "testing/test_live.py::test_remeasured": 1.5,
            "testing/test_live.py::test_deleted": 9.0,
            "testing/test_deleted.py::test_old": 9.0,
        },
    )
    partials = tmp_path / "partials" / "worker-1"
    _write_json(
        partials / "durations.json",
        {
            "testing/test_live.py::test_remeasured": 2.5,
            "testing/test_live.py::test_new": 3.5,
            "testing/test_live.py::test_partial_deleted": 4.5,
            "testing/test_live.py::test_outlier": merge_durations.OUTLIER_CAP + 1,
        },
    )

    assert merge_durations.merge(str(tmp_path / "partials"), str(output)) == 3

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_new": 3.5,
        "testing/test_live.py::test_remeasured": 2.5,
        "testing/test_live.py::test_untouched": 1.25,
    }


def test_merge_with_empty_partial_directory_keeps_baseline(tmp_path: Path) -> None:
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    baseline = {"testing/test_live.py::test_untouched": 1.25}
    _write_json(output, baseline)

    assert merge_durations.merge(str(tmp_path / "no-partials"), str(output)) == 1
    assert json.loads(output.read_text(encoding="utf-8")) == baseline


def test_regenerate_write_merged_preserves_baseline_and_filters_outliers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_test_file(tmp_path)
    _write_json(
        tmp_path / ".test_durations",
        {
            "testing/test_live.py::test_untouched": 1.25,
            "testing/test_live.py::test_remeasured": 1.5,
            "testing/test_deleted.py::test_old": 9.0,
        },
    )
    _write_json(
        tmp_path / ".durations_partial_1.json",
        {
            "testing/test_live.py::test_remeasured": 2.5,
            "testing/test_live.py::test_new": 3.5,
            "testing/test_live.py::test_outlier": merge_durations.OUTLIER_CAP + 1,
        },
    )
    regenerate = _load_regenerate_module()
    monkeypatch.chdir(tmp_path)

    assert regenerate.write_merged() == 3
    assert json.loads((tmp_path / ".test_durations").read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_new": 3.5,
        "testing/test_live.py::test_remeasured": 2.5,
        "testing/test_live.py::test_untouched": 1.25,
    }


def test_regenerate_coverage_counts_baseline_and_partial_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_file = tmp_path / "testing" / "test_live.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "\n".join(f"def test_{index}():\n    pass\n" for index in range(11))
        + "\ndef test_new():\n    pass\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / ".test_durations",
        {f"testing/test_live.py::test_{index}": index + 1.25 for index in range(11)},
    )
    _write_json(
        tmp_path / ".durations_partial_1.json",
        {"testing/test_live.py::test_new": 3.5},
    )
    regenerate = _load_regenerate_module()
    monkeypatch.chdir(tmp_path)

    covered, entries, files = regenerate.get_coverage()

    assert covered == 1
    assert entries == 12
    assert files == {"testing/test_live.py"}


# ── vitest telemetry generator (scripts/vitest-test-telemetry.mjs) ───────────

TELEMETRY_PATH = Path(__file__).with_name("vitest-test-telemetry.mjs")

_BUILD_HARNESS = """
import {{ buildArtifactFiles }} from {module!r};

const accumulated = {{
  'src/measured.test.ts': {{ duration_ms_sum: 300, tests: 3, passed_runs: 1, total_runs: 1 }},
  'src/fast.test.ts': {{ duration_ms_sum: 10, tests: 1, passed_runs: 1, total_runs: 1 }},
}};
const inventory = [
  'src/measured.test.ts',
  'src/fast.test.ts',
  'src/never-ran.test.ts',
  'src/also-never-ran.test.ts',
];
const result = buildArtifactFiles(accumulated, inventory, 1);
console.log(JSON.stringify(result));
"""


def _run_build_artifact_files() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on this host")
    script = _BUILD_HARNESS.format(module=str(TELEMETRY_PATH))
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_vitest_artifact_marks_unexecuted_files_unmeasured() -> None:
    """A file with no reporter result must never serialize as a real 0ms entry."""
    result = _run_build_artifact_files()

    by_file = {entry["file"]: entry for entry in result["files"]}
    assert by_file["src/measured.test.ts"]["measured"] is True
    assert by_file["src/measured.test.ts"]["duration_ms"] == 300

    for missing in ("src/never-ran.test.ts", "src/also-never-ran.test.ts"):
        assert by_file[missing]["measured"] is False, (
            f"{by_file[missing]}\n{format_offender_marker([missing])}"
        )
        assert by_file[missing]["duration_ms"] is None, (
            f"{by_file[missing]}\n{format_offender_marker([missing])}"
        )
        assert by_file[missing]["tests"] is None, (
            f"{by_file[missing]}\n{format_offender_marker([missing])}"
        )

    assert sorted(entry["file"] for entry in result["unmeasured"]) == [
        "src/also-never-ran.test.ts",
        "src/never-ran.test.ts",
    ]


def test_vitest_artifact_sorts_measured_first_then_unmeasured() -> None:
    """Unmeasured entries must not outrank (or undercut) real measurements."""
    files = _run_build_artifact_files()["files"]

    measured = [entry for entry in files if entry["measured"]]
    durations = [entry["duration_ms"] for entry in measured]
    assert durations == sorted(durations, reverse=True)
    assert [entry["measured"] for entry in files] == [True] * len(measured) + [False] * (
        len(files) - len(measured)
    )


def test_vitest_generator_exits_nonzero_on_unmeasured_files() -> None:
    """The generator must fail loudly rather than publish a partial artifact."""
    source = TELEMETRY_PATH.read_text(encoding="utf-8")
    assert "incomplete += (await generateArtifact(suite, SUITES[suite])).length;" in source
    assert "if (incomplete > 0) {" in source
    assert "process.exit(1);" in source
