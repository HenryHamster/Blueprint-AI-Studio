"""Execute the standalone ``scripts/test_*.py`` corpus in the CI test lane.

The promotion workflow already collects every module under ``backend/testing``.
Keeping one parametrized case per discovered script test lets pytest-split spread
the work across its normal shards without maintaining a second file roster.
Capability-limited files use the named exemptions owned by
``scripts/test-backend.sh``; an exemption is visible as a skipped case rather
than disappearing from collection.

``infra/db_recovery`` and ``infra/livekit/tests`` hold the same shape of
static-assertion ``test_*.py`` module but sit outside both ``scripts/`` and
``backend/testing/``. Neither ``.github/workflows/ci.yml`` (which only
collects ``backend/testing`` plus a hardcoded scripts list) nor this file
executed them before, so they ran in no CI lane at all. They are folded into
the same discover-and-subprocess loop below rather than exempted -- they need
no capability exemption, since they read only checked-in text.
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

# Additional roots discovered and executed the same way as SCRIPT_ROOT, minus
# the exemption mechanism: these are pure static-assertion tests (path reads +
# string asserts against checked-in IaC/config text), so there is no
# capability axis to exempt against.
INFRA_TEST_ROOTS = (
    REPOSITORY_ROOT / "infra" / "db_recovery",
    REPOSITORY_ROOT / "infra" / "livekit" / "tests",
)


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
    """Discover every script and infra test at collection time, in stable order."""
    paths = list(SCRIPT_ROOT.glob("test_*.py"))
    for infra_root in INFRA_TEST_ROOTS:
        paths.extend(infra_root.glob("test_*.py"))
    names = [path.name for path in paths]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, (
        "scripts/ and infra test roots must not share a filename (parametrize "
        f"ids and baseline-failure attribution key on the bare name): {duplicates}"
    )
    return tuple(sorted(paths))


SCRIPT_TEST_EXEMPTIONS = _script_test_exemptions()
SCRIPT_TEST_PATHS = _script_test_paths()


def _script_test_parameters() -> list[object]:
    # id=path.name (not the full relative path) preserves the pre-existing
    # test-id format: baseline-failure attribution correlates current and
    # baseline pytest node-ids by exact string match, and every discovered
    # filename here is already unique across SCRIPT_ROOT and INFRA_TEST_ROOTS.
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


# Per-file budget for the subprocess run below. 25 seconds keeps the lane
# honest for ordinary static-assertion script tests. test_local_runner.py is
# the one sanctioned outlier: it runs real subprocess integration checks
# (venv provisioning, one real `--check` boot probing isolation backends)
# whose combined cost on a loaded 2-vCPU CI runner exceeds the default.
# Its budget must stay ABOVE the 120-second timeout its real-boot setUpClass
# passes to subprocess.run, so a hung probe surfaces as that test's own
# TimeoutExpired (a diagnosable failure) instead of an opaque budget kill.
_FILE_BUDGET_SECONDS_DEFAULT = 25
_FILE_BUDGET_SECONDS_OVERRIDES = {
    "test_local_runner.py": 180,
}


@pytest.mark.parametrize("script_test", _script_test_parameters())
def test_script_test_module_executes(script_test: Path) -> None:
    """Run each non-exempt script test as a real pytest subprocess."""
    budget = _FILE_BUDGET_SECONDS_OVERRIDES.get(
        script_test.name, _FILE_BUDGET_SECONDS_DEFAULT
    )
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
            timeout=budget,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{script_test.name} exceeded the {budget}-second per-file test budget: "
            f"{_timeout_output(exc.stdout)}{_timeout_output(exc.stderr)}"
        )

    assert result.returncode == 0, (
        f"{script_test.name} failed in the scripts test lane\n"
        f"command: {' '.join(command)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_local_runner_is_not_blanket_exempt() -> None:
    """``scripts/test_local_runner.py`` must execute in this lane, not be skipped.

    The file spent its life behind the exemption reason "requires an enrolled
    local runner and its host services". Nothing in it enrolls a runner: it
    imports ``local_runner`` in-process and mocks the host. Behind the
    exemption it drifted to 20 failing tests — including one that had
    contradicted shipped behaviour since 2026-07-17 — and to a 49-second wall
    clock, while reporting green everywhere because it never ran.

    A file-level exemption is the wrong tool for a host-dependent CASE: it
    hides the other 640. When one test here genuinely needs a capability this
    lane lacks, mark that test with ``pytest.mark.skipif``.
    """
    assert "test_local_runner.py" not in SCRIPT_TEST_EXEMPTIONS, (
        "test_local_runner.py must run in the scripts test lane. If a single "
        "case needs a host capability, use pytest.mark.skipif on that case "
        "rather than re-adding a file-level exemption."
    )
    assert (SCRIPT_ROOT / "test_local_runner.py") in SCRIPT_TEST_PATHS, (
        "test_local_runner.py must still be discovered by the lane glob"
    )


def test_infra_test_roots_are_discovered_and_not_exempt() -> None:
    """``infra/db_recovery`` and ``infra/livekit/tests`` must execute here.

    Neither root sits under ``scripts/`` or ``backend/testing/``, and
    ``.github/workflows/ci.yml`` collects only those two locations (plus a
    hardcoded scripts subset) -- so before these roots were folded into this
    lane's discovery, their tests ran in zero CI lanes despite existing on
    disk with real assertions.
    """
    for infra_root in INFRA_TEST_ROOTS:
        discovered = set(infra_root.glob("test_*.py"))
        assert discovered, f"{infra_root} must contain at least one test_*.py"
        assert discovered <= set(SCRIPT_TEST_PATHS), (
            f"{infra_root} test modules must be discovered by the lane glob: "
            f"{sorted(p.name for p in discovered - set(SCRIPT_TEST_PATHS))}"
        )
        for path in discovered:
            assert path.name not in SCRIPT_TEST_EXEMPTIONS, (
                f"{path.name} is a pure static-assertion test; it must not "
                "carry a capability exemption"
            )
