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

import json
import os
import shlex
import subprocess
import sys
import tempfile
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

# -- hermetic credentials for every lane subprocess --------------------------
# Several scripts tests drive ``blueprint.main()`` for real. ``blueprint.py``
# does ``from blueprint_api import (...)``, so a double installed on the
# ``blueprint`` module does not intercept the copy of the same name that
# ``blueprint_api`` resolves internally: ``validate_project_id`` ->
# ``list_projects`` -> ``load_config`` -> ``api`` all run for real. The real
# ``load_config()`` search order ends at the repo-root and
# ``~/.blueprint-config.json`` files, so on a credentialed host this lane made
# live authenticated requests to the production orchestrator carrying the
# operator's admin key. CI containers hold no config file anywhere in that
# search order, which is exactly why CI could never catch it.
#
# ``scripts/conftest.py`` closes the same hole for direct ``pytest scripts/...``
# runs, but the subprocess below passes ``--noconftest``, so the guarantee has
# to travel through the environment instead. ``BLUEPRINT_CONFIG`` is tier 1 of
# ``load_config()`` and short-circuits the ambient files and the gcloud
# bootstrap; the URL/key pair is tier 2, for tests that deliberately clear
# tier 1. ``.invalid`` is reserved by RFC 6761 as guaranteed non-resolvable, so
# a request that escapes every mock still cannot reach a real host.
#
# backend/testing/test_scripts_lane_hermetic_credentials.py proves this
# environment defeats an ambient config and fails if it stops being applied.
HERMETIC_API_URL = "https://scripts-lane.blueprint.invalid"
HERMETIC_API_KEY = "hermetic-scripts-lane-not-a-credential"

_HERMETIC_CONFIG_PATH = (
    Path(tempfile.mkdtemp(prefix="blueprint-scripts-lane-hermetic-"))
    / "hermetic-blueprint-config.json"
)
_HERMETIC_CONFIG_PATH.write_text(
    json.dumps(
        {
            "api_url": HERMETIC_API_URL,
            "api_key": HERMETIC_API_KEY,
            "default_repo": "hermetic/scripts-lane",
            "default_base_branch": "main",
        }
    ),
    encoding="utf-8",
)


def hermetic_credential_environment() -> dict[str, str]:
    """Env overrides that stop a lane subprocess resolving a real credential."""
    return {
        "BLUEPRINT_CONFIG": str(_HERMETIC_CONFIG_PATH),
        "BLUEPRINT_API_URL": HERMETIC_API_URL,
        "BLUEPRINT_API_KEY": HERMETIC_API_KEY,
    }


def lane_subprocess_environment() -> dict[str, str]:
    """The exact environment every scripts-lane subprocess runs under."""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    pythonpath = [str(SCRIPT_ROOT), str(REPOSITORY_ROOT / "backend")]
    if environment.get("PYTHONPATH"):
        pythonpath.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
    environment.update(hermetic_credential_environment())
    return environment


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
            continue
        marks: list = []
        budget_override = _FILE_BUDGET_SECONDS_OVERRIDES.get(path.name)
        if budget_override is not None:
            marks.append(
                pytest.mark.timeout(budget_override + _OUTER_TIMEOUT_GRACE_SECONDS)
            )
        parameters.append(pytest.param(path, id=path.name, marks=marks))
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
    # 85 tests that drive scripts/static_release_guard.py end to end through a
    # fake `docker` shim -- each one forks the real guard, which forks pytest
    # and check_size_limits.py inside a throwaway fixture repo. Measured 42.7s
    # in the backend test image, so the 25s default is not survivable and the
    # cost is inherent to exercising a subprocess guard for real rather than
    # mocking the layer under test. Budgeted at ~3x measured for slower CI
    # hosts; a genuine wedge still trips it.
    "test_guard_gate_left_shift.py": 120,
    # 30 tests that fork real shells to exercise the VM bootstrap/retry shell
    # generators. Measured 31.73s in the backend test image (#17887), so the
    # 25s default was never survivable here -- it went red on CI shard 8 the
    # moment #17887 landed.
    #
    # The number is NOT sized against that measurement, though. This file
    # carries `@pytest.mark.timeout(120)` on
    # test_viewport_retry_distinguishes_infrastructure_from_child_exit, whose
    # worst case is a child that traps TERM and sleeps. A per-test marker of
    # 120s inside a file the lane only grants 25s is the SAME budget-inversion
    # class #17887 set out to close, just one layer further out: the marker
    # promises headroom this wrapper would refuse to honour. So the per-file
    # budget must clear the largest marker in the file, plus the same grace the
    # layers below use -- 120 + 60. test_scripts_lane_file_budgets_clear_their
    # _own_timeout_markers in backend/testing/test_harness_timeout_budgets.py
    # enforces exactly that relationship, so a future marker bump here fails
    # loudly instead of reddening a promotion PR.
    "test_github_actions_vm.py": 180,
    # Latent instance of the same inversion, found by the guard below rather
    # than by a red shard: test_real_probe_runs_live_sandbox_postgres_when
    # _installed boots a live sandbox PostgreSQL under an inner 90s budget and
    # carries @pytest.mark.timeout(180). It skips unless pg_config AND Ruff are
    # both present, which is why 25s has held so far -- on an image that has
    # them it would blow the per-file budget instead of running. 180 + 60.
    "test_runner_isolation.py": 240,
    # Real end-to-end compute-substrate checks: they build a throwaway Docker
    # image and run a container to prove `blueprint compute run` honours a
    # spec's image and setup_step. Measured 49.1s and 73.8s under CI conditions
    # (docker-dependent tests deselected), so the 25s default is not
    # survivable, and this file is deliberately NOT in test-backend.sh's
    # CI_SCRIPT_TEST_EXEMPTIONS -- the static half of it must keep running.
    #
    # As with the two entries above, the number is sized by the largest marker
    # in the file rather than by the measurement: three tests carry
    # @pytest.mark.timeout(300), so the per-file budget must clear 300 + the
    # same 60s grace the layers below use, or the marker promises headroom this
    # wrapper would refuse to honour. 360 also leaves ~5x headroom over the
    # slower measurement for a loaded 2-vCPU CI runner.
    "test_blueprint_compute_local.py": 360,
    # Two whole-tree walks of the real test estate: the ranked-queue generator's
    # and the classifier's. Measured 20.1s locally; the file carries
    # @pytest.mark.timeout(120) (~6x measured, sized for a loaded 2-vCPU runner
    # reading this tree over a bind mount), so the per-file budget must clear
    # 120 + the same 60s grace the layers below use.
    #
    # This was a latent inversion, not a red: both this file and the one below
    # arrived carrying 240s markers inside a lane that grants 25s. They are only
    # reachable from the standalone lane, so the mismatch would have surfaced
    # first at an automain->main promotion PR, where a red closes the PR.
    "test_derived_artifact_classify.py": 180,
    # One pass over the real 21k-row .test_durations artifact and the whole
    # backend test roster. Measured 9.6s; carries @pytest.mark.timeout(120) to
    # match its sibling above. Same 120 + 60 relationship.
    "test_prune_stale_durations.py": 180,
}

# CI's sharded pytest invocation passes ``--timeout=300`` (pytest-timeout) as a
# HANG DETECTOR, not as a performance budget -- see LANE_HANG_DETECTOR_SECONDS
# in backend/testing/test_harness_timeout_budgets.py. It was ``--timeout=30``,
# which killed the OUTER lane test before any override budget above 30s could
# matter. A ``pytest.mark.timeout`` marker takes precedence over the CLI option,
# so every override param still carries one at budget + this grace, which keeps
# the layers ordered even for the budgets above 300: the inner subprocess
# timeout fires first (diagnosable per-file failure with captured output), and
# the outer marker only fires if the lane harness itself wedges.
_OUTER_TIMEOUT_GRACE_SECONDS = 60


@pytest.mark.parametrize("script_test", _script_test_parameters())
def test_script_test_module_executes(script_test: Path) -> None:
    """Run each non-exempt script test as a real pytest subprocess."""
    budget = _FILE_BUDGET_SECONDS_OVERRIDES.get(
        script_test.name, _FILE_BUDGET_SECONDS_DEFAULT
    )
    environment = lane_subprocess_environment()

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
