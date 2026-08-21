"""Focused contract tests for the TypeScript baseline-attribution runner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import typecheck_verification_guard as guard  # noqa: E402

_INHERITED = "src/example.ts(12,5): error TS2345: Argument of type 'A' is not assignable to parameter of type 'B'."
_INHERITED_MOVED = "src/example.ts(84,9): error TS2345: Argument of type 'A' is not assignable to parameter of type 'B'."
_INTRODUCED = "src/new.ts(3,1): error TS2304: Cannot find name 'oops'."
_INHERITED_KEY = (
    "agent/src/example.ts::error TS2345: Argument of type 'A' is not assignable "
    "to parameter of type 'B'.::error"
)

_INVALID_EXIT_DIAGNOSTIC_PAIRS = [
    (0, _INHERITED),
    (1, ""),
    (2, ""),
    (137, ""),
]


@pytest.fixture(autouse=True)
def _isolate_verification_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Keep every in-process guard run out of the real durable sidecar.

    These tests drive ``guard.main()`` in-process, and the guard records its
    lane into the receipt resolved from ``BLUEPRINT_VERIFICATION_RECEIPT`` (or
    the ``TASK_ID`` fallback).  Without this, a corpus run of these tests --
    the static-corpus lane runs the whole ``scripts/test_*.py`` roster inside
    ./check, before the real typecheck lanes -- would write fixture lane blocks,
    with fabricated suppressed baseline failures, into the very receipt this
    gate treats as the single durable record.

    The cross-lane guard-suppression aggregate
    (``scripts/guard_suppression_receipt.py``) is pinned for the same reason:
    an in-process fixture run must not seed the real ``./check`` aggregate.
    """

    isolated = tmp_path_factory.mktemp("verification-receipt") / "receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(isolated))
    monkeypatch.setenv(
        "BLUEPRINT_GUARD_SUPPRESSION_DIR",
        str(tmp_path_factory.mktemp("guard-suppressions")),
    )
    monkeypatch.delenv("TASK_ID", raising=False)
    return isolated


def _stub_environment(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    *,
    current_output: str,
    base_output: str,
    current_exit: int | None = None,
    base_exit: int | None = None,
    current_files: set[str] | None = None,
    base_files: set[str] | None = None,
    changed_configs: list[str] | None = None,
    install_exit: int = 0,
) -> dict[str, object]:
    """Stub both compiles while preserving parsing and attribution logic."""

    calls: dict[str, object] = {"roots": [], "installs": 0}
    files = {
        "current": current_files if current_files is not None else {"agent/src/example.ts"},
        "base": base_files if base_files is not None else {"agent/src/example.ts"},
    }

    def fake_run_tsc(
        root: Path, suite: guard.Suite, args: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        is_current = root == repo
        if "--listFilesOnly" in args:
            listing = files["current" if is_current else "base"]
            return subprocess.CompletedProcess(
                [], 0, stdout="".join(f"{root / path}\n" for path in sorted(listing)), stderr=""
            )
        calls["roots"].append("current" if is_current else "base")
        output = current_output if is_current else base_output
        explicit = current_exit if is_current else base_exit
        exit_code = explicit if explicit is not None else (2 if output.strip() else 0)
        return subprocess.CompletedProcess([], exit_code, stdout=output, stderr="")

    def fake_install(*_: object) -> subprocess.CompletedProcess[str]:
        calls["installs"] = int(calls["installs"]) + 1
        return subprocess.CompletedProcess([], install_exit)

    monkeypatch.setattr(guard, "_run_tsc", fake_run_tsc)
    monkeypatch.setattr(guard, "_install_base", fake_install)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(
        guard, "_changed_program_configs", lambda *_: list(changed_configs or [])
    )
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    return calls


def _run(repo: Path, suite: str = "agent") -> int:
    return guard.main(["--suite", suite, "--base", "automain", "--repo-root", str(repo)])


def test_inherited_diagnostic_is_attributed_to_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A diagnostic present in both trees warn-passes and names its identity."""

    calls = _stub_environment(
        monkeypatch, tmp_path, current_output=_INHERITED, base_output=_INHERITED
    )

    assert _run(tmp_path) == 0
    assert calls["roots"] == ["current", "base"]
    assert calls["installs"] == 1
    output = capsys.readouterr().out
    assert f"WARN suppressed baseline diagnostic: {_INHERITED_KEY}" in output
    assert "current locations: 12:5" in output
    assert "PASS state=preexisting_type_failure" in output


def test_introduced_diagnostic_blocks_even_beside_an_inherited_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A diagnostic absent from the base compile is the task's own failure."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output=f"{_INHERITED}\n{_INTRODUCED}",
        base_output=_INHERITED,
        current_files={"agent/src/example.ts", "agent/src/new.ts"},
    )

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "FAILED state=type_failure new_diagnostic_count=1" in output
    assert "new diagnostic: agent/src/new.ts::error TS2304: Cannot find name 'oops'.::error" in output
    assert f"WARN suppressed baseline diagnostic: {_INHERITED_KEY}" in output


def test_inherited_diagnostic_that_moved_lines_keeps_its_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Line motion elsewhere in a file must not re-key an inherited diagnostic."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output=_INHERITED_MOVED,
        base_output=_INHERITED,
    )

    assert _run(tmp_path) == 0
    output = capsys.readouterr().out
    assert f"WARN suppressed baseline diagnostic: {_INHERITED_KEY}" in output
    assert "current locations: 84:9" in output
    assert "FAILED" not in output


def test_extra_identical_diagnostic_blocks_as_a_new_occurrence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second same-file TS diagnostic is new even though its position is not identity."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output=f"{_INHERITED}\n{_INHERITED_MOVED}",
        base_output=_INHERITED,
    )

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "WARN suppressed baseline diagnostic" in output
    assert (
        "new diagnostic: agent/src/example.ts::error TS2345: Argument of type 'A' is "
        "not assignable to parameter of type 'B'.::error::occurrence=2"
    ) in output
    assert "FAILED state=type_failure new_diagnostic_count=1" in output


def test_fixed_inherited_diagnostic_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A compiler emits a total diagnostic set, so a repaired base error is not a lost route."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output="",
        base_output=_INHERITED,
        changed_configs=["agent/tsconfig.json"],
    )

    assert _run(tmp_path) == 0
    assert "PASS state=passed" in capsys.readouterr().out


def test_clean_current_without_a_tsconfig_change_skips_the_base_compile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The program can only narrow through a tsconfig, so keep the cheap path."""

    calls = _stub_environment(
        monkeypatch, tmp_path, current_output="", base_output="", changed_configs=[]
    )

    assert _run(tmp_path) == 0
    assert calls["roots"] == ["current"]
    assert calls["installs"] == 0
    assert "PASS state=passed" in capsys.readouterr().out


def test_dropping_a_still_present_file_from_the_program_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Narrowing tsc's program must not launder an inherited diagnostic green."""

    (tmp_path / "agent" / "src").mkdir(parents=True)
    (tmp_path / "agent" / "src" / "example.ts").write_text("export {};\n", encoding="utf-8")

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output="",
        base_output=_INHERITED,
        changed_configs=["agent/tsconfig.json"],
        current_files=set(),
        base_files={"agent/src/example.ts"},
    )

    assert _run(tmp_path) == 1
    assert (
        "FAILED state=coverage_shrink files=agent/src/example.ts"
        in capsys.readouterr().out
    )


def test_deleting_a_file_is_not_treated_as_coverage_shrink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file the diff removed is legitimately gone from the checked program."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output="",
        base_output="",
        changed_configs=["agent/tsconfig.json"],
        current_files=set(),
        base_files={"agent/src/example.ts"},
    )

    assert _run(tmp_path) == 0
    assert "PASS state=passed" in capsys.readouterr().out


def test_unparseable_current_output_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """tsc output the runner cannot key must never be read as an absence of errors."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output="tsc: unexpected harness banner\n",
        base_output="",
    )

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "FAILED state=attribution_unavailable" in output
    assert "not parseable" in output


@pytest.mark.parametrize(
    ("exit_code", "output"),
    _INVALID_EXIT_DIAGNOSTIC_PAIRS,
    ids=[
        f"exit-{exit_code}-{'diagnostics' if output else 'clean'}"
        for exit_code, output in _INVALID_EXIT_DIAGNOSTIC_PAIRS
    ],
)
def test_current_exit_code_disagreeing_with_diagnostics_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
    output: str,
) -> None:
    """A crashed or silently-passing compiler cannot inherit a base verdict."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output=output,
        base_output="",
        current_exit=exit_code,
    )

    assert _run(tmp_path) == 1
    assert "FAILED state=execution_failure" in capsys.readouterr().out


def test_base_exit_code_disagreeing_with_diagnostics_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unusable base compile blocks instead of attributing against nothing."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output=_INHERITED,
        base_output="",
        base_exit=137,
    )

    assert _run(tmp_path) == 1
    assert "FAILED state=baseline_unavailable" in capsys.readouterr().out


def test_unresolvable_base_ref_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No base ref means no attribution, which must never be a silent pass."""

    _stub_environment(monkeypatch, tmp_path, current_output=_INHERITED, base_output="")

    def unresolvable(*_: object) -> str:
        raise RuntimeError("cannot resolve base ref origin/automain or automain")

    monkeypatch.setattr(guard, "_base_ref", unresolvable)

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "FAILED state=baseline_unavailable" in output
    assert "cannot resolve base ref" in output


def test_failed_base_install_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base checkout that cannot be installed is not an attributable baseline."""

    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output=_INHERITED,
        base_output="",
        install_exit=1,
    )

    assert _run(tmp_path) == 1
    assert (
        "FAILED state=baseline_unavailable base_install_exit_code=1"
        in capsys.readouterr().out
    )


def test_unusable_base_checkout_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base tree that cannot be extracted blocks rather than passing bare."""

    _stub_environment(monkeypatch, tmp_path, current_output=_INHERITED, base_output="")

    def broken_extract(*_: object) -> None:
        raise RuntimeError("git archive origin/automain failed (128)")

    monkeypatch.setattr(guard, "extract_base", broken_extract)

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "FAILED state=baseline_unavailable" in output
    assert "git archive origin/automain failed" in output


def test_unlistable_program_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Coverage that cannot be enumerated cannot be proven un-shrunk."""

    _stub_environment(monkeypatch, tmp_path, current_output=_INHERITED, base_output=_INHERITED)
    original = guard._run_tsc

    def fake_run_tsc(
        root: Path, suite: guard.Suite, args: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        if "--listFilesOnly" in args:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="TS5083: cannot read file")
        return original(root, suite, args)

    monkeypatch.setattr(guard, "_run_tsc", fake_run_tsc)

    assert _run(tmp_path) == 1
    assert "FAILED state=baseline_unavailable" in capsys.readouterr().out


def test_admin_ui_lane_covers_the_source_projects_and_the_test_project() -> None:
    """The lane replaces the shrink-only snapshot with a real test-project compile."""

    suite = guard.SUITES["admin-ui"]
    assert suite.check_commands == (
        ("-b", "tsconfig.json", "--noEmit"),
        ("-p", "tsconfig.test.json", "--noEmit"),
    )
    # Pin the argv actually handed to the compiler, not just the lane's args:
    # ``--pretty false`` ahead of ``-b`` silently disables build mode.
    assert [guard._tsc_argv(args) for args in suite.check_commands] == [
        ["npx", "tsc", "-b", "tsconfig.json", "--noEmit", "--pretty", "false"],
        ["npx", "tsc", "--pretty", "false", "-p", "tsconfig.test.json", "--noEmit"],
    ]
    assert "tsconfig.test.json" in suite.programs
    references = json.loads(
        guard._strip_jsonc((REPO_ROOT / "admin-ui" / "tsconfig.json").read_text(encoding="utf-8"))
    )["references"]
    assert sorted(entry["path"].removeprefix("./") for entry in references) == sorted(
        suite.source_programs
    )


def test_solution_reference_drift_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A newly referenced project must not silently sit outside the lane."""

    monkeypatch.setattr(
        guard,
        "_declared_references",
        lambda *_: ["tsconfig.app.json", "tsconfig.node.json", "tsconfig.extra.json"],
    )

    assert _run(tmp_path, suite="admin-ui") == 1
    assert "FAILED state=project_configuration_mismatch" in capsys.readouterr().out


def test_parse_diagnostics_preserves_multiplicity_without_position(tmp_path: Path) -> None:
    """Identity excludes position while retaining each repeated diagnostic occurrence."""

    output = (
        "src/example.ts(12,5): error TS2345: bad argument\n"
        "  src/other.ts(3,1): The expected type comes from here.\n"
        "src/example.ts(99,2): error TS2345: bad argument\n"
        "error TS18003: No inputs were found in config file.\n"
        "Found 3 errors in 2 files.\n"
    )

    parsed = guard._parse_diagnostics(
        output, project_root=tmp_path / "agent", tree_root=tmp_path, label="current"
    )

    assert sorted(parsed.report.failures) == [
        "<global>::error TS18003: No inputs were found in config file.::error",
        "agent/src/example.ts::error TS2345: bad argument::error",
        "agent/src/example.ts::error TS2345: bad argument::error::occurrence=2",
    ]
    assert parsed.locations[
        "agent/src/example.ts::error TS2345: bad argument::error"
    ] == ["12:5"]
    assert parsed.locations[
        "agent/src/example.ts::error TS2345: bad argument::error::occurrence=2"
    ] == ["99:2"]


def test_parse_diagnostics_routes_repeated_identical_failures_by_testcase(
    tmp_path: Path,
) -> None:
    """Repeated inherited diagnostics remain present testcases, not coverage loss."""

    output = (
        "src/example.ts(12,5): error TS2345: bad argument\n"
        "src/example.ts(99,2): error TS2345: bad argument\n"
    )
    current = guard._parse_diagnostics(
        output, project_root=tmp_path / "agent", tree_root=tmp_path, label="current"
    )
    baseline = guard._parse_diagnostics(
        output, project_root=tmp_path / "agent", tree_root=tmp_path, label="baseline"
    )

    attributed = guard.attribute_reports(current.report, baseline.report)

    assert current.report.test_routes == {
        "agent/src/example.ts::error TS2345: bad argument"
    }
    assert attributed.disappeared_failures == {}
    assert attributed.suppressed_failures == {
        "agent/src/example.ts::error TS2345: bad argument::error": (
            "agent/src/example.ts: error TS2345: bad argument"
        ),
        "agent/src/example.ts::error TS2345: bad argument::error::occurrence=2": (
            "agent/src/example.ts: error TS2345: bad argument"
        ),
    }


def test_parse_diagnostics_rejects_an_unrecognized_line(tmp_path: Path) -> None:
    """Fail closed on any non-indented line the runner cannot key."""

    with pytest.raises(RuntimeError, match="not parseable"):
        guard._parse_diagnostics(
            "src/example.ts:12:5 - error TS2345: bad argument\n",
            project_root=tmp_path / "agent",
            tree_root=tmp_path,
            label="current",
        )


def test_strip_jsonc_preserves_string_contents() -> None:
    """Comment stripping must not corrupt paths that contain comment markers."""

    assert json.loads(
        guard._strip_jsonc('{/* c */ "a": "http://x//y", // trailing\n "b": 1}')
    ) == {"a": "http://x//y", "b": 1}


@pytest.mark.parametrize("build_flag", ["-b", "--build"])
def test_build_mode_argv_keeps_the_build_flag_first(build_flag: str) -> None:
    """``isBuildCommand`` inspects argv[0] only, so nothing may precede ``-b``."""

    argv = guard._tsc_argv((build_flag, "tsconfig.json", "--noEmit"))

    assert argv[:3] == ["npx", "tsc", build_flag]
    assert argv[-2:] == ["--pretty", "false"]


def test_non_build_argv_still_carries_unstyled_output() -> None:
    """Every invocation must disable pretty output; parsing depends on it."""

    assert guard._tsc_argv(("-p", "tsconfig.json", "--listFilesOnly")) == [
        "npx",
        "tsc",
        "--pretty",
        "false",
        "-p",
        "tsconfig.json",
        "--listFilesOnly",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "error TS6369: Option '--build' must be the first command line argument.",
        "error TS5023: Unknown compiler option 'b'.",
        "error TS5094: Compiler option '--noEmit' may not be used with '--build'.",
    ],
)
def test_command_line_option_diagnostic_is_an_execution_failure(
    line: str, tmp_path: Path
) -> None:
    """An argv the compiler rejects must block, not attribute as inherited.

    Such a diagnostic reproduces identically in the base checkout, so treating
    it as a normal global diagnostic would warn-pass a lane that compiled
    nothing at all.
    """

    with pytest.raises(guard.ExecutionDisagreement, match="rejected its own command line"):
        guard._parse_diagnostics(
            f"{line}\n",
            project_root=tmp_path / "agent",
            tree_root=tmp_path,
            label="current",
        )


def test_a_rejected_command_line_blocks_the_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole lane fails closed when tsc never compiled anything."""

    rejected = "error TS6369: Option '--build' must be the first command line argument."
    _stub_environment(
        monkeypatch, tmp_path, current_output=rejected, base_output=rejected
    )

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "FAILED state=execution_failure" in output
    assert "rejected its own command line" in output


# --- end-to-end against the real compiler -----------------------------------

def _vendored_node_modules() -> Path | None:
    """Return a repo-local install that provides the TypeScript compiler."""

    for directory in ("admin-ui", "agent"):
        candidate = REPO_ROOT / directory / "node_modules"
        if (candidate / "typescript").is_dir() and (candidate / ".bin" / "tsc").exists():
            return candidate
    return None


_VENDORED_NODE_MODULES = _vendored_node_modules()

_FIXTURE_SOLUTION = '{"files": [], "references": [{"path": "./tsconfig.app.json"}]}'
_FIXTURE_LEAF = json.dumps(
    {
        "compilerOptions": {
            "composite": True,
            "strict": True,
            "skipLibCheck": True,
            "outDir": "dist",
            "types": [],
        },
        "include": ["src"],
    }
)


def _git_fixture(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "user.name=fixture",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _build_fixture_repo(root: Path) -> Path:
    """Create a two-commit repo whose HEAD introduces one real type error."""

    project = root / "fixture"
    (project / "src").mkdir(parents=True)
    (project / "package.json").write_text(
        '{"name": "typecheck-guard-fixture", "private": true}', encoding="utf-8"
    )
    (project / "tsconfig.json").write_text(_FIXTURE_SOLUTION, encoding="utf-8")
    (project / "tsconfig.app.json").write_text(_FIXTURE_LEAF, encoding="utf-8")
    (project / "src" / "ok.ts").write_text("export const value: number = 1;\n", encoding="utf-8")
    (root / ".gitignore").write_text("node_modules\ndist\n*.tsbuildinfo\n", encoding="utf-8")

    _git_fixture(root, "init", "--initial-branch=fixture-base")
    _git_fixture(root, "add", "-A")
    _git_fixture(root, "commit", "-m", "base")
    _git_fixture(root, "checkout", "-b", "fixture-head")
    (project / "src" / "broken.ts").write_text(
        'export const broken: number = "not a number";\n', encoding="utf-8"
    )
    _git_fixture(root, "add", "-A")
    _git_fixture(root, "commit", "-m", "head")
    (project / "node_modules").symlink_to(_VENDORED_NODE_MODULES, target_is_directory=True)
    return project


@pytest.mark.skipif(
    shutil.which("npx") is None or _VENDORED_NODE_MODULES is None,
    reason="requires node and a repo-local TypeScript install",
)
def test_end_to_end_build_lane_blocks_on_a_real_type_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run the real compiler: a build-mode lane must report an introduced error.

    Every other test in this file stubs ``_run_tsc``, so only this case proves
    the argv the runner emits actually compiles anything.  If build mode were
    disabled by argument order, tsc would reject its command line and the lane
    would never see ``src/broken.ts``.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    _build_fixture_repo(repo)

    def fake_install(root: Path, suite: guard.Suite) -> subprocess.CompletedProcess[str]:
        # ``git archive`` cannot carry the ignored install; stand in for npm ci.
        (root / suite.directory / "node_modules").symlink_to(
            _VENDORED_NODE_MODULES, target_is_directory=True
        )
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(guard, "_install_base", fake_install)
    monkeypatch.setitem(
        guard.SUITES,
        "fixture",
        guard.Suite(
            directory="fixture",
            install_directory="fixture",
            check_commands=(("-b", "tsconfig.json", "--noEmit"),),
            source_programs=("tsconfig.app.json",),
            solution="tsconfig.json",
        ),
    )

    exit_code = guard.main(
        ["--suite", "fixture", "--base", "fixture-base", "--repo-root", str(repo)]
    )
    output = capsys.readouterr().out

    assert "TS2322" in output, output
    assert "fixture/src/broken.ts" in output
    assert "FAILED state=type_failure" in output
    assert exit_code == 1


def _receipt_lanes(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8"))["lanes"]


def test_suppressed_baseline_diagnostics_are_written_to_the_shared_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The typecheck lane's suppressions outlive the VM as structured ids."""

    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(receipt_path))
    _stub_environment(
        monkeypatch, tmp_path, current_output=_INHERITED, base_output=_INHERITED
    )

    assert _run(tmp_path) == 0
    block = _receipt_lanes(receipt_path)["typecheck:agent"]
    assert block["state"] == "preexisting_type_failure"
    assert block["blocking"] is False
    assert block["new_failures"] == {}
    assert list(block["suppressed_failures"]) == [_INHERITED_KEY]
    assert (
        block["suppressed_failures"][_INHERITED_KEY]["detail"]
        == "agent/src/example.ts: error TS2345: Argument of type 'A' is not "
        "assignable to parameter of type 'B'."
    )


def test_a_blocking_typecheck_lane_still_records_its_attribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocked lane records what it introduced AND what it inherited."""

    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(receipt_path))
    _stub_environment(
        monkeypatch,
        tmp_path,
        current_output=f"{_INHERITED}\n{_INTRODUCED}",
        base_output=_INHERITED,
        current_files={"agent/src/example.ts", "agent/src/new.ts"},
    )

    assert _run(tmp_path) == 1
    block = _receipt_lanes(receipt_path)["typecheck:agent"]
    assert block["state"] == "type_failure"
    assert block["blocking"] is True
    assert list(block["new_failures"]) == [
        "agent/src/new.ts::error TS2304: Cannot find name 'oops'.::error"
    ]
    assert list(block["suppressed_failures"]) == [_INHERITED_KEY]


def test_an_unwritable_receipt_does_not_change_the_typecheck_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Receipt writing is advisory: a broken sidecar keeps the lane green."""

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(blocked / "receipt.json"))
    _stub_environment(
        monkeypatch, tmp_path, current_output=_INHERITED, base_output=_INHERITED
    )

    assert _run(tmp_path) == 0
    captured = capsys.readouterr()
    assert "PASS state=preexisting_type_failure" in captured.out
    assert "could not record lane typecheck:agent" in captured.err


def test_the_cheap_clean_path_still_records_a_passing_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lane that suppressed nothing is still named in the receipt."""

    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(receipt_path))
    _stub_environment(
        monkeypatch, tmp_path, current_output="", base_output="", changed_configs=[]
    )

    assert _run(tmp_path) == 0
    block = _receipt_lanes(receipt_path)["typecheck:agent"]
    assert block["state"] == "passed"
    assert block["suppressed_failures"] == {}


def test_a_guard_test_run_leaves_the_gate_resolved_sidecar_untouched(
    tmp_path: Path,
) -> None:
    """Running this module must not write into the real durable receipt.

    The static-corpus lane runs the whole ``scripts/test_*.py`` roster inside
    ./check, before the real typecheck lanes and with the gate's TASK_ID in the
    environment.  If these tests resolved the ambient sidecar, they would
    publish fixture lane blocks -- including fabricated suppressed baseline
    failures -- as durable evidence for lanes that never ran.
    """

    task_id = f"receipt-isolation-{tmp_path.name}"
    sidecar = Path("/tmp") / f"project_verification_receipt_{task_id}.json"
    sentinel = '{"version": 1, "lanes": {}}\n'
    sidecar.write_text(sentinel, encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("BLUEPRINT_VERIFICATION_RECEIPT", None)
    environment["TASK_ID"] = task_id
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(Path(__file__).resolve()),
                "-q",
                "-p",
                "no:cacheprovider",
                "-k",
                "not leaves_the_gate_resolved_sidecar_untouched",
            ],
            cwd=str(REPO_ROOT),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert sidecar.read_text(encoding="utf-8") == sentinel
    finally:
        sidecar.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Cross-lane guard-suppression aggregate (scripts/guard_suppression_receipt.py)
# ---------------------------------------------------------------------------
#
# The publication receipt above is this lane's own durable record.  The gate
# ALSO merges every guard lane -- static, vitest, typecheck, viewport, backend
# pytest -- into one task-scoped aggregate that `./pr` renders.  A typecheck
# lane that skipped that aggregate would be invisible there even though it
# forgave inherited breakage, so it is asserted independently.


def _aggregate_lanes(directory: Path) -> dict[str, dict]:
    return {
        json.loads(path.read_text(encoding="utf-8"))["lane"]: json.loads(
            path.read_text(encoding="utf-8")
        )
        for path in sorted(directory.glob("lane_*.json"))
    }


def test_lane_name_matches_the_publication_receipt_lane_identity() -> None:
    """One suite must not land under two identities in the two sidecars."""

    assert guard.lane_name("agent") == "typecheck:agent"
    assert guard.lane_name("agent") == guard.typecheck_lane("agent")


def test_suppressions_reach_the_cross_lane_guard_aggregate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    aggregate_dir = tmp_path / "guard-suppressions"
    monkeypatch.setenv("BLUEPRINT_GUARD_SUPPRESSION_DIR", str(aggregate_dir))
    _stub_environment(
        monkeypatch, tmp_path, current_output=_INHERITED, base_output=_INHERITED
    )

    assert _run(tmp_path) == 0
    record = _aggregate_lanes(aggregate_dir)["typecheck:agent"]
    assert record["state"] == "preexisting_type_failure"
    assert list(record["buckets"]["suppressed"]["entries"]) == [_INHERITED_KEY]


def test_the_cheap_clean_path_also_reaches_the_guard_aggregate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A green lane still names itself, so a missing lane means a missing run."""

    aggregate_dir = tmp_path / "guard-suppressions"
    monkeypatch.setenv("BLUEPRINT_GUARD_SUPPRESSION_DIR", str(aggregate_dir))
    _stub_environment(monkeypatch, tmp_path, current_output="", base_output="")

    assert _run(tmp_path) == 0
    record = _aggregate_lanes(aggregate_dir)["typecheck:agent"]
    assert record["state"] == "passed"
    assert record["buckets"]["suppressed"]["entries"] == {}


def test_an_unwritable_guard_aggregate_does_not_change_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Emitting is fail-open: the aggregate can never block a lane."""

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("BLUEPRINT_GUARD_SUPPRESSION_DIR", str(blocked / "nested"))
    _stub_environment(
        monkeypatch, tmp_path, current_output=_INHERITED, base_output=_INHERITED
    )

    assert _run(tmp_path) == 0


def test_a_rewrapped_diagnostic_message_keeps_one_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Internal whitespace is collapsed, so re-wrapping is not a new failure."""

    rewrapped = (
        "src/example.ts(12,5): error TS2345: Argument of type 'A' is  not "
        "assignable  to parameter of type 'B'."
    )
    _stub_environment(
        monkeypatch, tmp_path, current_output=rewrapped, base_output=_INHERITED
    )

    assert _run(tmp_path) == 0
    output = capsys.readouterr().out
    assert "new diagnostic" not in output
    assert f"WARN suppressed baseline diagnostic: {_INHERITED_KEY}" in output
