"""Regression tests for the backend duration artifact generators."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
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
        "def test_new():\n    pass\n\n"
        "def test_post_gap():\n    pass\n\n"
        + "\n".join(
            f"def test_{index}():\n    pass\n" for index in range(100)
        ),
        encoding="utf-8",
    )


def _write_store_duration_harness(root: Path) -> tuple[Path, Path]:
    """Create a tiny backend runner that records the script's argv contract."""
    runner = root / "mock-test-backend.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$@\" >> \"$DURATION_ARGS_LOG\"\n"
        "cd \"$DURATION_BACKEND_DIR\"\n"
        "path=.test_durations\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --durations-path ]; then path=\"$2\"; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        "sleep \"${DURATION_RUNNER_DELAY:-0}\"\n"
        "mkdir -p \"$(dirname \"$path\")\"\n"
        "printf '{\\\"%s\\\": %s}\\n' \"${DURATION_RESULT_KEY:-testing/test_live.py::test_remeasured}\" \"${DURATION_RESULT_VALUE:-2.5}\" > \"$path\"\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    log = root / "duration-runner-args.txt"
    return runner, log


def _run_store_duration_script(
    root: Path, runner: Path, log: Path, *targets: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "DURATION_BACKEND_DIR": str(root),
            "DURATION_TEST_BACKEND_SCRIPT": str(runner),
            "DURATION_ARGS_LOG": str(log),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(Path(__file__).with_name("store-backend-durations.sh")), *targets],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_store_backend_durations_targeted_refresh_forwards_targets_and_merges(
    tmp_path: Path,
) -> None:
    """Explicit selectors write a partial file and merge into the old artifact."""
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    runner, log = _write_store_duration_harness(tmp_path)

    result = _run_store_duration_script(
        tmp_path, runner, log, "testing/test_live.py::test_remeasured"
    )

    assert result.returncode == 0, result.stderr + result.stdout
    args = log.read_text(encoding="utf-8").splitlines()
    assert args[:3] == [
        "testing/test_live.py::test_remeasured",
        "--store-durations",
        "--durations-path",
    ]
    assert len(args) == 4
    assert re.fullmatch(r"\.duration_refresh\.[A-Za-z0-9]+/target/durations\.json", args[3])
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_remeasured": 2.5,
        "testing/test_live.py::test_untouched": 1.25,
    }
    assert not list(tmp_path.glob(".duration_refresh.??????"))


def test_store_backend_durations_targeted_refreshes_are_isolated_and_serialized(
    tmp_path: Path,
) -> None:
    """Concurrent targeted runs preserve both results and a pre-existing directory."""
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    preserved = tmp_path / ".duration_refresh"
    preserved.mkdir()
    marker = preserved / "keep"
    marker.write_text("do not remove", encoding="utf-8")
    runner, log = _write_store_duration_harness(tmp_path)

    command = [
        "bash",
        str(Path(__file__).with_name("store-backend-durations.sh")),
        "testing/test_live.py",
    ]
    base_env = os.environ.copy()
    base_env.update(
        {
            "DURATION_BACKEND_DIR": str(tmp_path),
            "DURATION_TEST_BACKEND_SCRIPT": str(runner),
            "DURATION_ARGS_LOG": str(log),
            "DURATION_RUNNER_DELAY": "0.2",
        }
    )
    first_env = base_env | {"DURATION_RESULT_KEY": "testing/test_live.py::test_new"}
    second_env = base_env | {"DURATION_RESULT_KEY": "testing/test_live.py::test_post_gap"}
    first = subprocess.Popen(command, cwd=tmp_path, env=first_env, text=True)
    time.sleep(0.05)
    second = subprocess.Popen(command, cwd=tmp_path, env=second_env, text=True)

    assert first.wait(timeout=20) == 0
    assert second.wait(timeout=20) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_new": 2.5,
        "testing/test_live.py::test_post_gap": 2.5,
        "testing/test_live.py::test_untouched": 1.25,
    }
    assert marker.read_text(encoding="utf-8") == "do not remove"
    paths = [
        value
        for value in log.read_text(encoding="utf-8").splitlines()
        if value.startswith(".duration_refresh.")
    ]
    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert not list(tmp_path.glob(".duration_refresh.??????"))


def test_store_backend_durations_zero_arguments_preserve_full_refresh(
    tmp_path: Path,
) -> None:
    """No selectors retain the full testing/ invocation and direct artifact write."""
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    runner, log = _write_store_duration_harness(tmp_path)

    result = _run_store_duration_script(tmp_path, runner, log)

    assert result.returncode == 0, result.stderr + result.stdout
    assert log.read_text(encoding="utf-8").splitlines() == [
        "testing/",
        "--store-durations",
    ]
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_remeasured": 2.5,
    }


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


def test_merge_drops_observed_setup_plateau_but_keeps_first_post_gap_sample(
    tmp_path: Path,
) -> None:
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    plateau = {
        f"testing/test_live.py::test_{index}": 12.0 + index / 1000
        for index in range(100)
    }
    plateau["testing/test_live.py::test_post_gap"] = 27.43031809399872
    _write_json(output, plateau)

    assert merge_durations.merge(str(tmp_path / "no-partials"), str(output)) == 1
    merged = json.loads(output.read_text(encoding="utf-8"))
    assert merged == {
        "testing/test_live.py::test_post_gap": 27.43031809399872,
    }


def test_merge_keeps_plateau_when_an_observation_is_inside_diagnostic_gap(
    tmp_path: Path,
) -> None:
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    plateau = {
        f"testing/test_live.py::test_{index}": 12.0 + index / 1000
        for index in range(100)
    }
    plateau["testing/test_live.py::test_post_gap"] = 20.0
    _write_json(output, plateau)

    assert merge_durations.merge(str(tmp_path / "no-partials"), str(output)) == 101
    assert json.loads(output.read_text(encoding="utf-8")) == plateau


def _fresh_setup_plateau() -> dict[str, float]:
    """A freshly observed setup-attribution plateau, sized to the detector.

    Exactly IN_BAND_SETUP_MIN_COUNT entries inside the 12-15s band, plus the
    real first post-gap measurement so the fresh observations carry their own
    evidence that the diagnostic gap is empty.
    """
    plateau = {
        f"testing/test_live.py::test_{index}": 12.0 + index / 1000
        for index in range(100)
    }
    plateau["testing/test_live.py::test_post_gap"] = 27.43031809399872
    return plateau


def test_merge_drops_a_fresh_plateau_a_baseline_gap_sample_would_have_vouched_for(
    tmp_path: Path,
) -> None:
    """Fresh observations are judged on their own evidence, not the baseline's.

    The baseline holds a 22.0s measurement, which sits inside the diagnostic
    gap. Swept over baseline-and-fresh together that historical sample makes
    the freshly recorded plateau look legitimate, and the merge persists 100
    setup artifacts as the new baseline, where nothing can tell them from real
    measurements afterwards.
    """
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 22.0})
    _write_json(
        tmp_path / "partials" / "worker-1" / "durations.json", _fresh_setup_plateau()
    )

    assert merge_durations.merge(str(tmp_path / "partials"), str(output)) == 2
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 22.0,
        "testing/test_live.py::test_post_gap": 27.43031809399872,
    }


def test_merge_keeps_a_fresh_plateau_when_the_fresh_data_fills_the_gap(
    tmp_path: Path,
) -> None:
    """Judging fresh data alone must stay as conservative as the merged sweep."""
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 22.0})
    partial = _fresh_setup_plateau()
    partial["testing/test_live.py::test_post_gap"] = 20.0
    _write_json(tmp_path / "partials" / "worker-1" / "durations.json", partial)

    assert merge_durations.merge(str(tmp_path / "partials"), str(output)) == 102
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 22.0,
        **partial,
    }


def test_merge_drops_entries_from_an_unparseable_test_module(tmp_path: Path) -> None:
    """Unreadable identities fail closed rather than preserving stale durations."""
    test_file = tmp_path / "testing" / "test_broken.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_broken(\n", encoding="utf-8")

    assert merge_durations.filter_current_entries(
        {"testing/test_broken.py::test_broken": 1.0}, tmp_path
    ) == {}


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


def test_regenerate_write_merged_drops_a_fresh_plateau_the_baseline_would_vouch_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch regenerator must reject a fresh plateau on the same evidence."""
    _write_test_file(tmp_path)
    _write_json(
        tmp_path / ".test_durations", {"testing/test_live.py::test_untouched": 22.0}
    )
    _write_json(tmp_path / ".durations_partial_1.json", _fresh_setup_plateau())
    regenerate = _load_regenerate_module()
    monkeypatch.chdir(tmp_path)

    assert regenerate.write_merged() == 2
    assert json.loads((tmp_path / ".test_durations").read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 22.0,
        "testing/test_live.py::test_post_gap": 27.43031809399872,
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
