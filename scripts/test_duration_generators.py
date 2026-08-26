"""Regression tests for the backend duration artifact generators."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import merge_durations
import posix_bash
from testing.fixgate_attribution import format_offender_marker


REGENERATE_PATH = Path(__file__).with_name("regenerate-durations.py")
SCRIPTS_DIR = Path(__file__).resolve().parent
STORE_SCRIPT = SCRIPTS_DIR / "store-backend-durations.sh"
BATCH_SCRIPT = SCRIPTS_DIR / "batch-store-durations.sh"
MERGE_SCRIPT = SCRIPTS_DIR / "merge_durations.py"
PYTHON_HELPER = SCRIPTS_DIR / "python-interpreter.sh"


def _bash() -> str:
    """The bash these tests must drive the shell scripts with.

    A bare ``"bash"`` argv[0] is resolved by ``CreateProcess``, which searches
    ``%SystemRoot%\\System32`` before PATH -- the WSL launcher on a Windows
    host, which starts a different filesystem with a fresh environment block
    and drops every ``DURATION_*`` variable these tests pass by ``env=``.
    """
    return posix_bash.bash_executable()


def _sh(path: str | os.PathLike[str]) -> str:
    """Render *path* the way the shell under test has to receive it.

    On Windows, ``subprocess`` hands the child one command-line STRING and
    bash re-parses it, so the backslashes in ``C:\\dir\\file`` are consumed as
    escapes and the script is reported missing. The identity on POSIX, where a
    backslash is a legal filename character and must never be rewritten.
    """
    return posix_bash.posix_path(path)


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
    env = _store_duration_environment(root, runner, log)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [_bash(), _sh(STORE_SCRIPT), *targets],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _store_duration_environment(root: Path, runner: Path, log: Path) -> dict[str, str]:
    """The harness contract, in the path form the shell script consumes."""
    env = os.environ.copy()
    env.update(
        {
            "DURATION_BACKEND_DIR": _sh(root),
            "DURATION_TEST_BACKEND_SCRIPT": _sh(runner),
            "DURATION_ARGS_LOG": _sh(log),
        }
    )
    # A developer host may export PYTHON to pin an interpreter; these tests
    # assert the script's own resolution, so it must not leak in.
    env.pop("PYTHON", None)
    return env


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

    command = [_bash(), _sh(STORE_SCRIPT), "testing/test_live.py"]
    base_env = _store_duration_environment(tmp_path, runner, log)
    base_env["DURATION_RUNNER_DELAY"] = "0.2"
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
    # A file:// URL, not a bare path: an ESM specifier that starts with a
    # Windows drive letter is not a relative/absolute specifier node accepts,
    # so `import ... from 'D:\\...'` fails to resolve on a Windows host. The
    # URL form is what node documents and is identical on POSIX.
    script = _BUILD_HARNESS.format(module=TELEMETRY_PATH.as_uri())
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


# ── mutual exclusion around the shared duration artifact ─────────────────────
#
# The merge is a read-modify-write of ONE committed file and several producers
# reach it. scripts/store-backend-durations.sh used to serialize it with
# `flock(1)` -- a binary Git Bash on Windows does not ship. There, a targeted
# refresh ran its tests to completion, died at the merge with
# `flock: command not found` (exit 127), and the cleanup trap then deleted the
# raw measurement: backend/.test_durations was never written, and the operator
# saw a passing test run scroll past a single-line error. The serialization now
# lives inside scripts/merge_durations.py, where it costs no external binary
# and covers every caller on every platform.

_LOCK_HOLDER_SOURCE = '''"""Take the duration-artifact lock, announce it, and hold until killed."""

import sys
import time

sys.path.insert(0, sys.argv[1])

import merge_durations

with merge_durations.duration_artifact_lock(sys.argv[2], timeout=20):
    print("HELD", flush=True)
    time.sleep(300)
'''


def _partial(root: Path, entries: dict[str, float]) -> Path:
    """Write one measurement in the ``<src>/*/*.json`` layout merge() globs."""
    src_root = root / "partials"
    _write_json(src_root / "target" / "durations.json", entries)
    return src_root


def _merge_command(src_root: Path, output: Path) -> list[str]:
    return [sys.executable, str(MERGE_SCRIPT), str(src_root), str(output)]


def _merge_env(lock_timeout: str) -> dict[str, str]:
    env = os.environ.copy()
    env[merge_durations.LOCK_TIMEOUT_ENV] = lock_timeout
    return env


def test_two_concurrent_merges_both_land(tmp_path: Path) -> None:
    """Overlapping merges serialize; neither measurement may be dropped."""
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    first_src = _partial(tmp_path / "first", {"testing/test_live.py::test_new": 2.5})
    second_src = _partial(
        tmp_path / "second", {"testing/test_live.py::test_post_gap": 3.5}
    )

    # Hold the artifact so BOTH children are guaranteed to arrive while it is
    # taken. That is the interleaving two real refreshes hit, and the one an
    # unsynchronized merge loses a measurement in: both would read the same
    # baseline and the last writer would discard the other's entry.
    with merge_durations.duration_artifact_lock(output):
        first = subprocess.Popen(
            _merge_command(first_src, output),
            env=_merge_env("60"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        second = subprocess.Popen(
            _merge_command(second_src, output),
            env=_merge_env("60"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(1.0)
        still_waiting = (first.poll(), second.poll())
        assert still_waiting == (None, None), (
            "a merge completed while the artifact lock was held, so it never "
            f"took the lock and can overwrite a concurrent measurement: {still_waiting}"
        )

    first_output, _ = first.communicate(timeout=20)
    second_output, _ = second.communicate(timeout=20)
    assert first.returncode == 0, first_output
    assert second.returncode == 0, second_output
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 1.25,
        "testing/test_live.py::test_new": 2.5,
        "testing/test_live.py::test_post_gap": 3.5,
    }


def test_a_lock_file_left_by_an_earlier_run_does_not_break_a_merge(
    tmp_path: Path,
) -> None:
    """The sidecar is expected to already exist, and is never truncated."""
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    lock_file = Path(merge_durations.lock_path_for(output))
    lock_file.write_bytes(b"left behind by an earlier run\n")
    src_root = _partial(tmp_path, {"testing/test_live.py::test_new": 2.5})

    assert merge_durations.merge(str(src_root), str(output)) == 2

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 1.25,
        "testing/test_live.py::test_new": 2.5,
    }
    assert lock_file.read_bytes() == b"left behind by an earlier run\n"


def test_a_held_lock_raises_a_named_timeout_rather_than_hanging(
    tmp_path: Path,
) -> None:
    """The wait is bounded, and the error names the file it waited on."""
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    src_root = _partial(tmp_path, {"testing/test_live.py::test_new": 2.5})

    started = time.monotonic()
    with merge_durations.duration_artifact_lock(output):
        with pytest.raises(merge_durations.DurationLockTimeout) as excinfo:
            merge_durations.merge(str(src_root), str(output), lock_timeout=0.2)
    waited = time.monotonic() - started

    message = str(excinfo.value)
    assert merge_durations.lock_path_for(output) in message, message
    assert "0.2" in message, message
    assert waited < 30, f"the bounded wait took {waited:.1f}s"
    # The merge did not happen -- and did not half-happen either.
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 1.25
    }


def test_the_merge_cli_reports_a_lock_timeout_instead_of_skipping(
    tmp_path: Path,
) -> None:
    """Contention surfaces as a distinct non-zero exit, never a silent no-op.

    A merge that quietly declined would discard a measurement that cost a full
    Docker+Postgres run -- the same loss the flock defect caused.
    """
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    src_root = _partial(tmp_path, {"testing/test_live.py::test_new": 2.5})

    with merge_durations.duration_artifact_lock(output):
        result = subprocess.run(
            _merge_command(src_root, output),
            env=_merge_env("0.3"),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    assert result.returncode == 2, result.stdout + result.stderr
    assert merge_durations.lock_path_for(output) in result.stderr, result.stderr
    assert "timed out" in result.stderr, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 1.25
    }


def test_a_lock_abandoned_by_a_killed_holder_is_recovered(tmp_path: Path) -> None:
    """A crashed merge must not wedge every merge that follows it.

    The lock is an OS advisory lock, so the kernel drops it when the holding
    process dies. Nothing has to detect or repair a stale lock, and the sidecar
    file itself is deliberately left on disk.
    """
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    src_root = _partial(tmp_path, {"testing/test_live.py::test_new": 2.5})
    holder_script = tmp_path / "hold_the_lock.py"
    holder_script.write_text(_LOCK_HOLDER_SOURCE, encoding="utf-8")

    holder = subprocess.Popen(
        [sys.executable, str(holder_script), str(SCRIPTS_DIR), str(output)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"
        # Non-vacuity: while the holder lives the lock really is exclusive, so
        # the recovery below cannot be passing for want of any lock at all.
        blocked = subprocess.run(
            _merge_command(src_root, output),
            env=_merge_env("0.3"),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    finally:
        holder.kill()
        holder.wait(timeout=20)

    assert merge_durations.merge(str(src_root), str(output), lock_timeout=10) == 2
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 1.25,
        "testing/test_live.py::test_new": 2.5,
    }
    assert Path(merge_durations.lock_path_for(output)).exists()


def test_store_backend_durations_keeps_the_measurement_when_the_merge_fails(
    tmp_path: Path,
) -> None:
    """A failed merge must not destroy a run that cost 15-20 minutes.

    The cleanup trap used to delete the measurement directory unconditionally,
    so the flock failure took the raw timings with it and left re-running the
    entire suite as the only recovery.
    """
    _write_test_file(tmp_path)
    output = tmp_path / ".test_durations"
    _write_json(output, {"testing/test_live.py::test_untouched": 1.25})
    runner, log = _write_store_duration_harness(tmp_path)

    # `false` becomes the merge interpreter, so the merge invocation exits
    # non-zero: exactly the shape of the failure being guarded against.
    result = _run_store_duration_script(
        tmp_path, runner, log, "testing/test_live.py", extra_env={"PYTHON": "false"}
    )

    assert result.returncode != 0, result.stdout
    retained = list(tmp_path.glob(".duration_refresh.*"))
    assert len(retained) == 1, (
        f"{retained}\n{result.stdout}\n{result.stderr}\n"
        + format_offender_marker(sorted(str(path) for path in retained))
    )
    assert (retained[0] / "target" / "durations.json").is_file()
    assert retained[0].name in result.stderr, result.stderr
    assert "merge_durations.py" in result.stderr, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "testing/test_live.py::test_untouched": 1.25
    }


# ── host portability of the duration shell scripts ───────────────────────────

# A command word, as opposed to a mention inside a comment or a diagnostic
# message. Mirrors the command-word shape scripts/test_developer_host_gate.py
# pins for scripts/check-default.sh.
_ABSENT_ON_WINDOWS = re.compile(r"(?:^|[|;&(]|\s)(flock|python3)\s")

# scripts/python-interpreter.sh is deliberately out of scope: probing for a
# `python3` on PATH is the one job it exists to do.
_DURATION_SHELL_SCRIPTS = (STORE_SCRIPT, BATCH_SCRIPT)


def test_the_duration_shell_scripts_call_no_binary_git_bash_lacks() -> None:
    """Neither `flock` nor `python3` exists in Git Bash on Windows.

    The host interpreter is resolved through scripts/python-interpreter.sh and
    the merge serializes itself in Python, so no executed line here may name
    either binary. A `python3` handed to `_run` is exempt: that word is argv
    inside the Linux test container, where python3 is the correct spelling.
    """
    offenders: list[str] = []
    offending_scripts: set[str] = set()
    for script in _DURATION_SHELL_SCRIPTS:
        for lineno, line in enumerate(
            script.read_text(encoding="utf-8").splitlines(), 1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            match = _ABSENT_ON_WINDOWS.search(line)
            if match is None:
                continue
            if match.group(1) == "python3" and stripped.startswith("_run "):
                continue
            offenders.append(f"scripts/{script.name}:{lineno}: {stripped}")
            offending_scripts.add(f"scripts/{script.name}")

    assert not offenders, (
        "these lines invoke a binary a Windows/Git-Bash host does not have, so "
        "the duration merge dies there after the tests have already run:\n"
        + "\n".join(offenders)
        + "\n"
        + format_offender_marker(sorted(offending_scripts))
    )


def _resolve_interpreter(
    path_entry: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run scripts/python-interpreter.sh against a controlled PATH."""
    env = os.environ.copy()
    # Exactly one entry: an os.pathsep join would use ';' on Windows, which an
    # MSYS shell does not split PATH on.
    env["PATH"] = _sh(path_entry)
    env.pop("PYTHON", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [_bash(), "-c", f'. "{_sh(PYTHON_HELPER)}"; blueprint_python_interpreter'],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )


def _interpreter_dir(tmp_path: Path, *names: str) -> Path:
    """A PATH directory holding exactly the named interpreter shims."""
    directory = tmp_path / ("bin-" + "-".join(names or ("none",)))
    directory.mkdir()
    for name in names:
        shim = directory / name
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


def test_the_interpreter_falls_back_to_python_where_no_python3_exists(
    tmp_path: Path,
) -> None:
    """The python.org Windows installer publishes python.exe and no python3."""
    result = _resolve_interpreter(_interpreter_dir(tmp_path, "python"))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python", result.stdout


def test_the_interpreter_prefers_python3_where_both_exist(tmp_path: Path) -> None:
    """A POSIX host keeps resolving exactly what it resolved before."""
    result = _resolve_interpreter(_interpreter_dir(tmp_path, "python3", "python"))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python3", result.stdout


def test_the_interpreter_fails_closed_and_names_the_cause(tmp_path: Path) -> None:
    """No interpreter at all is an error that says so, not a wrong answer."""
    result = _resolve_interpreter(_interpreter_dir(tmp_path))

    assert result.returncode != 0
    assert "neither python3 nor python is on PATH" in result.stderr, result.stderr
    assert result.stdout.strip() == ""


def test_the_interpreter_honours_an_explicit_override(tmp_path: Path) -> None:
    """PYTHON pins an interpreter on a host that keeps it somewhere unusual."""
    result = _resolve_interpreter(
        _interpreter_dir(tmp_path, "python3"), {"PYTHON": "/opt/pinned/python3.11"}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/opt/pinned/python3.11", result.stdout
