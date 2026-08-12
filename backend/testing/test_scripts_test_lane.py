"""Execute the standalone ``scripts/test_*.py`` corpus in the CI test lane.

The promotion workflow already collects every module under ``backend/testing``.
Keeping one parametrized case per discovered script test lets pytest-split spread
the work across its normal shards without maintaining a second file roster.
Capability-limited files use the named exemptions owned by
``scripts/test-backend.sh``; an exemption is visible as a skipped case rather
than disappearing from collection.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


def _repository_root() -> Path:
    """Resolve both the local checkout and CI's separate ``/repo`` mount."""
    candidates = []
    configured_root = os.environ.get("BLUEPRINT_REPO_ROOT")
    if configured_root:
        candidates.append(Path(configured_root))
    candidates.extend((Path("/repo"), Path(__file__).resolve().parents[2]))
    for candidate in candidates:
        if (candidate / "scripts").is_dir():
            return candidate
    raise RuntimeError("could not resolve repository root containing scripts/")


REPOSITORY_ROOT = _repository_root()
SCRIPT_ROOT = REPOSITORY_ROOT / "scripts"
HARNESS = SCRIPT_ROOT / "test-backend.sh"


def _script_test_exemptions() -> dict[str, str]:
    """Read the one explicit capability-exemption list from the shell harness."""
    source = HARNESS.read_text(encoding="utf-8")
    marker = "CI_SCRIPT_TEST_EXEMPTIONS=("
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"{HARNESS} must define {marker[:-1]}")
    body_start = start + len(marker)
    body_end = source.find("\n)", body_start)
    if body_end < 0:
        raise AssertionError(
            f"{HARNESS} CI_SCRIPT_TEST_EXEMPTIONS must have a closing parenthesis"
        )

    exemptions: dict[str, str] = {}
    for raw_line in source[body_start:body_end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        values = shlex.split(line, comments=True)
        if len(values) != 1:
            raise AssertionError(f"invalid script test exemption entry: {line!r}")
        filename, separator, reason = values[0].partition("|")
        if not separator or not reason.strip():
            raise AssertionError(
                f"script test exemption {values[0]!r} must include a reason"
            )
        if filename in exemptions:
            raise AssertionError(f"duplicate script test exemption: {filename}")
        exemptions[filename] = reason.strip()
    return exemptions


def _script_test_paths() -> tuple[Path, ...]:
    """Discover every script test at collection time, in stable order."""
    return tuple(sorted(SCRIPT_ROOT.glob("test_*.py")))


SCRIPT_TEST_EXEMPTIONS = _script_test_exemptions()
SCRIPT_TEST_PATHS = _script_test_paths()


def _script_test_parameters() -> list[object]:
    parameters: list[object] = []
    for path in SCRIPT_TEST_PATHS:
        reason = SCRIPT_TEST_EXEMPTIONS.get(path.name)
        if reason:
            parameters.append(
                pytest.param(
                    path,
                    id=path.name,
                    marks=pytest.mark.skip(reason=f"capability exemption: {reason}"),
                )
            )
        else:
            parameters.append(pytest.param(path, id=path.name))
    return parameters


def _timeout_output(value: str | bytes | None) -> str:
    """Render subprocess timeout output without leaking bytes repr into errors."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


@pytest.mark.parametrize("script_test", _script_test_parameters())
def test_script_test_module_executes(script_test: Path) -> None:
    """Run each non-exempt script test as a real pytest subprocess."""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    pythonpath = [str(SCRIPT_ROOT), str(REPOSITORY_ROOT / "backend")]
    if environment.get("PYTHONPATH"):
        pythonpath.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)

    command = [
        sys.executable,
        "-m",
        "pytest",
        str(script_test),
        "--noconftest",
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{script_test.name} exceeded the 25-second per-file test budget: "
            f"{_timeout_output(exc.stdout)}{_timeout_output(exc.stderr)}"
        )

    assert result.returncode == 0, (
        f"{script_test.name} failed in the scripts test lane\n"
        f"command: {' '.join(command)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
