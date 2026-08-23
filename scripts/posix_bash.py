#!/usr/bin/env python3
r"""Resolve the POSIX shell and path forms a guard must hand to the harness.

Two Windows-host facts silently break any guard that shells out to
``scripts/test-backend.sh`` and passes its contract through the
environment:

*   ``subprocess`` resolves a bare ``"bash"`` argv[0] through
    ``CreateProcess``, which searches ``%SystemRoot%\System32`` *before*
    ``PATH``.  Wherever WSL is installed that directory holds
    ``bash.exe`` -- the WSL launcher.  It starts a Linux distribution
    with a fresh environment block, so every variable the caller passed
    by ``env=`` is dropped: the harness sees no selector list and no
    JUnit path, runs the entire standalone corpus instead of the scoped
    subset, writes no report, and the guard then fails with a
    missing-file error that names none of that.

*   A Windows ``Path`` renders as ``C:\dir\file``.  ``dirname`` in an
    MSYS shell splits on ``/`` only, so it answers ``.`` for that
    string; the harness then mounts the wrong directory and pytest
    writes its report somewhere the caller never looks.

Both are silent-wrong-answer failures, not errors, which is why they
survive: the run looks like it happened.  Resolve both explicitly here
and fail closed with the actual reason when no usable shell exists.
"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path

_WSL_LAUNCHER_REMEDY = (
    "the only bash on PATH is the Windows System32 WSL launcher, which does "
    "not inherit this process's environment; install Git for Windows (its "
    r"bash is at 'C:\Program Files\Git\bin\bash.exe'), run the gate from "
    "inside WSL, or point BLUEPRINT_BASH at a bash that inherits env"
)


def _is_windows_host() -> bool:
    """Whether this process runs on Windows.

    Isolated behind a function so the Windows branches below stay reachable
    from a POSIX test runner: patching ``os.name`` itself would also switch
    ``pathlib`` to ``WindowsPath`` and break the interpreter mid-test.
    """

    return os.name == "nt"


def _is_wsl_launcher(candidate: Path) -> bool:
    """Report whether *candidate* is the Windows System32 WSL launcher."""

    if not _is_windows_host():
        return False
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if not system_root:
        return False
    try:
        candidate.resolve().relative_to((Path(system_root) / "System32").resolve())
    except (OSError, ValueError):
        return False
    return True


def _path_candidates(name: str) -> list[Path]:
    """Return every executable *name* on PATH, in PATH order."""

    filenames = (f"{name}.exe", name) if _is_windows_host() else (name,)
    found: list[Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        for filename in filenames:
            candidate = Path(entry) / filename
            if candidate.is_file():
                found.append(candidate)
    return found


def bash_executable() -> str:
    """Return an absolute bash that inherits this process's environment.

    ``BLUEPRINT_BASH`` overrides the search for a host that keeps its
    shell somewhere unusual.
    """

    override = os.environ.get("BLUEPRINT_BASH")
    if override:
        return override
    rejected: list[str] = []
    for candidate in _path_candidates("bash"):
        if _is_wsl_launcher(candidate):
            rejected.append(str(candidate))
            continue
        return str(candidate)
    if rejected:
        raise RuntimeError(
            f"cannot run the harness: {_WSL_LAUNCHER_REMEDY} "
            f"(rejected: {', '.join(rejected)})"
        )
    raise RuntimeError("cannot run the harness: no bash on PATH")


def posix_path(path: str | os.PathLike[str]) -> str:
    """Render *path* the way an MSYS/Git-Bash shell has to receive it."""

    text = os.fspath(path)
    if not _is_windows_host():
        return text
    drive, rest = ntpath.splitdrive(text)
    rest = rest.replace("\\", "/")
    if len(drive) == 2 and drive[1] == ":":
        return f"/{drive[0].lower()}{rest}"
    return rest or text
