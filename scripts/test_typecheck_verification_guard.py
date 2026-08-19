"""Focused contract tests for the tsc baseline-attribution runner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import typecheck_verification_guard as guard  # noqa: E402


def test_parse_tsc_failures_keys_on_file_code_and_message_not_line() -> None:
    message = "Type 'string' is not assignable to type 'number'."
    output = f"src/App.tsx(12,5): error TS2322: {message}\nFound 1 error.\n"
    parsed = guard.parse_tsc_failures(output)
    assert parsed.failures == {f"src/App.tsx:TS2322:{message}": message}
    assert parsed.locations == {f"src/App.tsx:TS2322:{message}": ("src/App.tsx(12)",)}
    assert parsed.unparsed == ()
    assert (parsed.parsed_lines, parsed.total_lines) == (1, 1)

    # The same diagnostic on a shifted line keeps its identity.
    shifted = guard.parse_tsc_failures(f"src/App.tsx(13,5): error TS2322: {message}\n")
    assert set(shifted.failures) == set(parsed.failures)


def test_parse_tsc_failures_ignores_non_diagnostic_lines() -> None:
    assert guard.parse_tsc_failures("").failures == {}
    empty = guard.parse_tsc_failures("Found 0 errors.\n")
    assert (empty.failures, empty.unparsed, empty.total_lines) == ({}, (), 0)


def test_parse_tsc_failures_reports_unanchored_diagnostics() -> None:
    """A file-less `error TS...` line is counted, never silently discarded."""

    parsed = guard.parse_tsc_failures(
        "src/App.tsx(12,5): error TS2322: bad assignment.\n"
        "error TS18003: No inputs were found in config file 'tsconfig.json'.\n"
    )
    assert parsed.total_lines == 2
    assert parsed.parsed_lines == 1
    assert parsed.unparsed == (
        "error TS18003: No inputs were found in config file 'tsconfig.json'.",
    )


def test_clean_current_run_passes_without_a_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected(*_: object, **__: object) -> None:
        pytest.fail("a clean current run must not touch the baseline")

    monkeypatch.setattr(
        guard,
        "_run_tsc",
        lambda *_: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(guard, "_base_ref", unexpected)
    monkeypatch.setattr(guard, "extract_base", unexpected)
    monkeypatch.setattr(guard, "_install", unexpected)

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 0
    assert "PASS state=passed" in capsys.readouterr().out


def test_current_failure_with_no_baseline_blocks_as_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A genuinely new type error (clean base) still blocks the task."""

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        if root == tmp_path:
            return subprocess.CompletedProcess(
                [],
                1,
                stdout="src/App.tsx(12,5): error TS2322: bad assignment.\n",
                stderr="",
            )
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install", lambda *_: subprocess.CompletedProcess([], 0))

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "FAILED state=test_failure" in output


def test_current_failure_identical_to_baseline_warn_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A diagnostic inherited unchanged from the base ref does not block."""

    diagnostic = "src/App.tsx(12,5): error TS2322: bad assignment.\n"

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout=diagnostic, stderr="")

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install", lambda *_: subprocess.CompletedProcess([], 0))

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert (
        "WARN suppressed baseline failure: src/App.tsx:TS2322:bad assignment. "
        "at src/App.tsx(12)" in output
    )
    assert "PASS state=preexisting_test_failure" in output


def test_inherited_diagnostic_shifted_by_an_edit_above_it_warn_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Editing above a pre-existing diagnostic must not blame this task for it."""

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        line = 13 if root == tmp_path else 12
        return subprocess.CompletedProcess(
            [], 1, stdout=f"src/App.tsx({line},5): error TS2322: bad assignment.\n", stderr=""
        )

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install", lambda *_: subprocess.CompletedProcess([], 0))

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "PASS state=preexisting_test_failure" in output
    assert "FAILED" not in output


def test_new_unanchored_diagnostic_beside_an_inherited_one_blocks_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unattributable diagnostic must not ride along with inherited breakage."""

    def unexpected(*_: object, **__: object) -> None:
        pytest.fail("an unattributable current diagnostic must not reach the baseline")

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [],
            1,
            stdout=(
                "src/App.tsx(12,5): error TS2322: bad assignment.\n"
                "error TS18003: No inputs were found in config file 'tsconfig.json'.\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", unexpected)
    monkeypatch.setattr(guard, "extract_base", unexpected)
    monkeypatch.setattr(guard, "_install", unexpected)

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "FAILED state=unattributable_diagnostic" in output
    assert "parsed_diagnostics=1 total_diagnostics=2" in output
    assert "unattributable diagnostic: error TS18003" in output


def test_partially_fixing_inherited_breakage_passes_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vanished tsc diagnostic means it was fixed; log it, do not block."""

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        if root == tmp_path:
            return subprocess.CompletedProcess(
                [], 1, stdout="src/A.tsx(1,1): error TS1111: still broken.\n", stderr=""
            )
        return subprocess.CompletedProcess(
            [],
            1,
            stdout=(
                "src/A.tsx(1,1): error TS1111: still broken.\n"
                "src/B.tsx(2,2): error TS2222: was broken.\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install", lambda *_: subprocess.CompletedProcess([], 0))

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert (
        "NOTE baseline diagnostic no longer reported: src/B.tsx:TS2222:was broken. "
        "at src/B.tsx(2)" in output
    )
    assert "PASS state=baseline_failure_disappeared" in output


def test_new_diagnostic_alongside_a_disappeared_one_still_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fixing one inherited error does not buy a pass for a newly added one."""

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        if root == tmp_path:
            return subprocess.CompletedProcess(
                [], 1, stdout="src/C.tsx(3,3): error TS3333: brand new.\n", stderr=""
            )
        return subprocess.CompletedProcess(
            [], 1, stdout="src/B.tsx(2,2): error TS2222: was broken.\n", stderr=""
        )

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install", lambda *_: subprocess.CompletedProcess([], 0))

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "new diagnostic: src/C.tsx:TS3333:brand new. at src/C.tsx(3)" in output
    assert "FAILED state=test_failure" in output


def test_unattributable_baseline_diagnostic_blocks_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An incomplete baseline failure set cannot establish inherited breakage."""

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        if root == tmp_path:
            return subprocess.CompletedProcess(
                [], 1, stdout="src/A.tsx(1,1): error TS1111: broken.\n", stderr=""
            )
        return subprocess.CompletedProcess(
            [],
            1,
            stdout=(
                "src/A.tsx(1,1): error TS1111: broken.\n"
                "error TS5083: Cannot read file 'tsconfig.json'.\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install", lambda *_: subprocess.CompletedProcess([], 0))

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "FAILED state=baseline_unavailable" in output
    assert "unattributable baseline diagnostic: error TS5083" in output


def test_current_execution_failure_with_no_diagnostics_blocks_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A nonzero exit with unparseable output is an infra failure, not attributable."""

    def unexpected(*_: object, **__: object) -> None:
        pytest.fail("an unparseable current failure must not reach the baseline")

    monkeypatch.setattr(
        guard,
        "_run_tsc",
        lambda *_: subprocess.CompletedProcess([], 2, stdout="", stderr="internal error"),
    )
    monkeypatch.setattr(guard, "_base_ref", unexpected)
    monkeypatch.setattr(guard, "extract_base", unexpected)
    monkeypatch.setattr(guard, "_install", unexpected)

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    assert "FAILED state=execution_failure" in capsys.readouterr().out


def test_base_ref_unavailable_blocks_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(*_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 1, stdout="src/App.tsx(1,1): error TS1234: broken.\n", stderr=""
        )

    def fake_base_ref(*_: object) -> str:
        raise RuntimeError("cannot resolve base ref")

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", fake_base_ref)

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    assert "FAILED state=baseline_unavailable" in capsys.readouterr().out


def test_base_install_failure_blocks_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(*_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 1, stdout="src/App.tsx(1,1): error TS1234: broken.\n", stderr=""
        )

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(
        guard, "_install", lambda *_: subprocess.CompletedProcess([], 1)
    )

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    assert "FAILED state=baseline_unavailable" in capsys.readouterr().out


def test_base_typecheck_unparseable_failure_blocks_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A baseline run that fails without any parseable diagnostic can't attribute."""

    def fake_run(root: Path, _: guard.Suite) -> subprocess.CompletedProcess[str]:
        if root == tmp_path:
            return subprocess.CompletedProcess(
                [], 1, stdout="src/App.tsx(1,1): error TS1234: broken.\n", stderr=""
            )
        return subprocess.CompletedProcess([], 1, stdout="", stderr="internal error")

    monkeypatch.setattr(guard, "_run_tsc", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install", lambda *_: subprocess.CompletedProcess([], 0))

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    assert "FAILED state=baseline_unavailable" in capsys.readouterr().out


def test_generated_scripts_call_the_attributed_typecheck_guard() -> None:
    """Both the generated self-repo script and the static default carry the lane."""

    default_script = (REPO_ROOT / "scripts" / "check-default.sh").read_text(encoding="utf-8")
    command = (
        'run "admin-ui typecheck (attributed)" '
        "python3 scripts/typecheck_verification_guard.py --suite admin-ui "
        '--base "$BASE" --repo-root "$WORKSPACE"'
    )
    assert command in default_script
