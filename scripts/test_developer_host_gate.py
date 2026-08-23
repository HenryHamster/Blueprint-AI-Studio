"""The publication gate has to be able to EXECUTE on a developer host.

Promotion PR #17764 fail-closed on four stale checked-in guard-data
manifests.  Every guard that catches them already runs in the standalone
lane, and all four PRs that left them stale (#17756, #17757, #17758,
#17761) were authored in ``bp/*`` worktrees on a Windows host.  On that
host ``./check`` did not merely skip a lane: it could not run at all.

Two host facts caused that, and both are silent-wrong-answer bugs rather
than errors, which is why they survived:

* Git Bash on Windows has no ``python3`` -- the python.org installer
  publishes ``python.exe`` only -- so every python-backed lane in
  ``scripts/check-default.sh`` died with ``python3: command not found``.
* ``subprocess`` resolves a bare ``"bash"`` argv[0] through
  ``CreateProcess``, which searches ``%SystemRoot%\\System32`` before
  ``PATH``.  Where WSL is installed that is the WSL launcher, which
  starts a Linux distribution with a fresh environment block and drops
  the selector list and JUnit path the static guard passes by ``env=``.

A gate nobody can run is a gate nobody runs.  These tests pin that the
gate resolves an interpreter and a shell that honour its own contract,
and that it fails closed -- naming the cause -- when it cannot.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# scripts/*.py tests run with --noconftest, so the shared offender-identity
# producer is reached by the same explicit path insertion the other scripts
# guards use rather than by a conftest-installed sys.path entry.
sys.path.insert(0, str(REPO_ROOT / "backend"))

import posix_bash  # noqa: E402
import static_release_guard as guard  # noqa: E402
from testing.fixgate_attribution import format_offender_marker  # noqa: E402

_CHECK = REPO_ROOT / "check"
_DEFAULT_GATE = REPO_ROOT / "scripts" / "check-default.sh"

# A python3 command word, as opposed to a mention inside a message or comment.
_INVOKES_PYTHON3 = re.compile(r"(?:^|[|;&(]|\s)python3\s")

# Everything ``scripts/check-default.sh`` reaches for before it reports, so a
# sandbox PATH can omit an interpreter without also omitting the shell.
_SANDBOX_TOOLS = (
    "awk", "basename", "bash", "cat", "chmod", "cp", "cut", "date", "diff",
    "dirname", "env", "expr", "find", "git", "grep", "head", "id", "ln", "ls",
    "mkdir", "mktemp", "mv", "readlink", "realpath", "rm", "sed", "sh", "sort",
    "stat", "tail", "tee", "timeout", "touch", "tr", "uname", "uniq", "wc",
    "xargs",
)


def _sandbox_bin(tmp_path: Path, *, with_python: bool) -> Path:
    """Build a PATH directory that has no ``python3`` on it, by construction."""

    sandbox = tmp_path / "sandbox-bin"
    sandbox.mkdir()
    for tool in _SANDBOX_TOOLS:
        resolved = shutil.which(tool)
        if resolved:
            (sandbox / tool).symlink_to(resolved)
    if with_python:
        shim = sandbox / "python"
        shim.write_text(
            '#!/bin/sh\nexec "' + sys.executable + '" "$@"\n', encoding="utf-8"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return sandbox


def _init_gate_repo(tmp_path: Path) -> Path:
    """A throwaway checkout carrying the tracked gate and zero-exit helpers."""

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(_CHECK, repo / "check")
    (repo / "check").chmod((repo / "check").stat().st_mode | stat.S_IXUSR)
    gate = repo / "scripts" / "check-default.sh"
    shutil.copy2(_DEFAULT_GATE, gate)
    gate.chmod(gate.stat().st_mode | stat.S_IXUSR)
    shutil.copy2(
        REPO_ROOT / "scripts" / "backend-test-route.sh",
        repo / "scripts" / "backend-test-route.sh",
    )
    for helper in ("static_release_guard.py", "sentinel_freshness.py"):
        (repo / "scripts" / helper).write_text(
            "raise SystemExit(0)\n", encoding="utf-8"
        )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (
        ("user.email", "gate-test@example.com"),
        ("user.name", "gate test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "branch", "-f", "automain"], check=True)
    (repo / "file.txt").write_text("hello there\n", encoding="utf-8")
    return repo


def _run_gate(repo: Path, sandbox: Path) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash, "the host running these tests must have bash"
    tmpdir = repo.parent / "tmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        "PATH": str(sandbox),
        "HOME": str(repo.parent),
        "WORKSPACE": str(repo),
        "TMPDIR": str(tmpdir),
    }
    return subprocess.run(
        [bash, str(repo / "check")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.skipif(os.name == "nt", reason="the sandbox PATH needs POSIX symlinks")
def test_the_gate_runs_where_only_python_is_installed(tmp_path: Path) -> None:
    """A host with ``python`` but no ``python3`` must still run every lane.

    This is the Windows/Git-Bash shape of the incident host reproduced on a
    POSIX runner: the gate's own call sites all say ``python3``.
    """

    repo = _init_gate_repo(tmp_path)
    sandbox = _sandbox_bin(tmp_path, with_python=True)
    result = _run_gate(repo, sandbox)
    combined = result.stdout + "\n" + result.stderr

    assert "python3: command not found" not in combined, combined
    assert "routing python3 to" in combined, combined
    assert "static size guards (task diff) OK" in combined, combined
    assert (
        "derived-artifact freshness (registered drift sentinels) OK" in combined
    ), combined


@pytest.mark.skipif(os.name == "nt", reason="the sandbox PATH needs POSIX symlinks")
def test_the_gate_fails_closed_when_no_interpreter_exists(tmp_path: Path) -> None:
    """No python at all is a hard stop that names the cause, not a green run."""

    repo = _init_gate_repo(tmp_path)
    sandbox = _sandbox_bin(tmp_path, with_python=False)
    result = _run_gate(repo, sandbox)
    combined = result.stdout + "\n" + result.stderr

    assert result.returncode == 2, combined
    assert "neither python3 nor python is on PATH" in combined, combined
    assert "PASS" not in combined, combined


def test_the_gate_publishes_the_shim_before_its_first_python_call_site() -> None:
    """The shim is worthless if any lane resolves ``python3`` ahead of it."""

    lines = _DEFAULT_GATE.read_text(encoding="utf-8").splitlines()
    block_start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("if ! command -v python3 ")
    )
    block_end = next(
        index for index, line in enumerate(lines) if index > block_start and line == "fi"
    )
    assert any(
        "routing python3 to" in line for line in lines[block_start:block_end]
    ), "the shim block must announce the interpreter it routed to"

    call_sites = [
        index
        for index, line in enumerate(lines)
        if _INVOKES_PYTHON3.search(line) and not line.lstrip().startswith("#")
    ]
    assert call_sites, "this test is vacuous if the gate stopped invoking python3"
    early = [index for index in call_sites if index < block_start]
    assert not early, (
        "a python3 call site precedes the shim that publishes python3: "
        "line " + str(early[0] + 1) + " is " + repr(lines[early[0]])
        + "\n"
        + format_offender_marker(str(index + 1) for index in early)
    )


def test_bash_resolution_skips_the_windows_wsl_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """System32's ``bash.exe`` is the WSL launcher and must never be chosen.

    It starts a Linux distribution with a fresh environment block, so the
    selector list and JUnit path the guard passes by ``env=`` never arrive:
    the harness runs the whole corpus and writes no report.
    """

    system_root = tmp_path / "Windows"
    (system_root / "System32").mkdir(parents=True)
    (system_root / "System32" / "bash.exe").write_text("", encoding="utf-8")
    git_bin = tmp_path / "Git" / "usr" / "bin"
    git_bin.mkdir(parents=True)
    (git_bin / "bash.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(posix_bash, "_is_windows_host", lambda: True)
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(system_root / "System32"), str(git_bin)])
    )
    monkeypatch.delenv("BLUEPRINT_BASH", raising=False)

    assert posix_bash.bash_executable() == str(git_bin / "bash.exe")


def test_bash_resolution_fails_closed_when_only_the_wsl_launcher_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing beats running the wrong shell for fifteen unattributable minutes."""

    system_root = tmp_path / "Windows"
    (system_root / "System32").mkdir(parents=True)
    (system_root / "System32" / "bash.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(posix_bash, "_is_windows_host", lambda: True)
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setenv("PATH", str(system_root / "System32"))
    monkeypatch.delenv("BLUEPRINT_BASH", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        posix_bash.bash_executable()
    message = str(excinfo.value)
    assert "WSL launcher" in message, message
    assert "does not inherit this process's environment" in message, message
    assert "BLUEPRINT_BASH" in message, message


def test_bash_resolution_honours_an_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that keeps bash somewhere unusual gets an escape hatch."""

    monkeypatch.setenv("BLUEPRINT_BASH", "/opt/toolchain/bin/bash")
    assert posix_bash.bash_executable() == "/opt/toolchain/bin/bash"


def test_windows_paths_are_rendered_for_an_msys_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dirname`` splits on ``/`` only, so a backslash path mounts the wrong dir."""

    monkeypatch.setattr(posix_bash, "_is_windows_host", lambda: True)
    assert (
        posix_bash.posix_path("C:\\Users\\dev\\Temp\\current.xml")
        == "/c/Users/dev/Temp/current.xml"
    )


def test_posix_paths_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posix_bash, "_is_windows_host", lambda: False)
    assert posix_bash.posix_path("/tmp/guard/current.xml") == "/tmp/guard/current.xml"


def test_the_static_guard_never_shells_out_to_a_bare_bash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_run`` is the single process boundary every harness call crosses."""

    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        seen["command"] = list(command)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "bash_executable", lambda: "/resolved/bin/bash")
    guard._run(["bash", "scripts/test-backend.sh"], cwd=tmp_path, env={})

    assert seen["command"] == ["/resolved/bin/bash", "scripts/test-backend.sh"]


def test_the_static_corpus_hands_the_harness_a_shell_readable_junit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The harness ``dirname``s this value to pick the report mount."""

    seen: dict[str, object] = {}

    def fake_run(command, *, cwd, env):  # type: ignore[no-untyped-def]
        seen["junit"] = env["BLUEPRINT_STATIC_GUARD_JUNIT"]
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(guard, "_run", fake_run)
    monkeypatch.setattr(guard, "posix_path", lambda value: "/posix" + str(value))
    guard._run_static_corpus(tmp_path, tmp_path / "current.xml", selectors=["a.py"])

    assert seen["junit"] == "/posix" + str(tmp_path / "current.xml")
