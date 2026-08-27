"""Guard: the ``scripts/`` test lane must never resolve a real credential.

``scripts/test_*.py`` are unit tests, but several drive ``blueprint.main()``
across the real argparse/dispatch boundary. ``blueprint.py`` does
``from blueprint_api import (api, get_project_id, list_projects, load_config,
validate_project_id, ...)``, so each of those names exists in two module
namespaces. A double installed as ``monkeypatch.setattr(blueprint, "api", ...)``
rebinds only ``blueprint``'s copy; the calls ``blueprint_api`` makes internally
(``validate_project_id`` -> ``list_projects`` -> ``load_config`` -> ``api``)
still reach the real functions.

The real ``blueprint_api.load_config()`` search order ends at the repo-root and
``~/.blueprint-config.json`` files, and ``_bootstrap_from_gcloud()`` will mint a
fresh admin key out of Secret Manager and cache it to the home directory. So on
a credentialed developer machine the lane made live authenticated HTTPS requests
to the PRODUCTION orchestrator carrying the operator's admin key, and printed
the real project catalog into pytest's captured stderr. Measured: 24 requests
across ``test_hermes_operator_cli.py`` (21) and
``test_blueprint_fast_claim_repair_cli.py`` (3).

CI containers hold no config file anywhere in that search order, so the lane is
green there and CI can NEVER catch this class on its own. That is why every
check below either reads checked-in text or manufactures its own throwaway
"ambient" config: the guard is just as sharp on an empty CI container as it is
on a credentialed workstation.

Three layers keep the lane hermetic, and this file asserts all three:

1. ``scripts/conftest.py`` -- direct ``pytest scripts/...`` runs.
2. ``backend/testing/test_scripts_test_lane.py`` -- the CI lane subprocess,
   which passes ``--noconftest`` and so must carry the guarantee in its
   environment.
3. ``scripts/test-backend.sh`` -- the standalone harness, likewise
   ``--noconftest``.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from testing.fixgate_attribution import format_offender_marker
from testing.test_scripts_test_lane import (
    HERMETIC_API_KEY,
    HERMETIC_API_URL,
    SCRIPT_ROOT,
    SCRIPT_TEST_PATHS,
    lane_subprocess_environment,
)

SHELL_HARNESS = SCRIPT_ROOT / "test-backend.sh"
SCRIPTS_CONFTEST = SCRIPT_ROOT / "conftest.py"

# RFC 6761 reserves ``.invalid`` as guaranteed non-resolvable. Every hermetic
# credential this repo hands the scripts lane must point there, so a request
# that escapes every test double still cannot reach a real host. This literal
# is the security-relevant pin, written here independently of the values the
# three layers choose, so a layer that quietly switched to a routable host
# fails this file instead of certifying itself.
NON_ROUTABLE_SUFFIX = ".invalid"

# The names ``blueprint.py`` re-binds via ``from blueprint_api import (...)``.
# A test that patches one of these on ``blueprint`` alone has NOT mocked the
# call ``blueprint_api`` makes internally.
DUAL_BOUND_NAMES = (
    "api",
    "get_project_id",
    "list_projects",
    "load_config",
    "resolve_project_id_by_name",
    "validate_project_id",
)

# Modules measured making live authenticated requests to the production
# orchestrator from a credentialed host. Pinned by name so a regression that
# reintroduces the single-binding mock in either one stays attributable.
HISTORICALLY_LEAKING_MODULES = (
    "test_hermes_operator_cli.py",
    "test_blueprint_fast_claim_repair_cli.py",
)

AMBIENT_KEY = "ambient-operator-key"

# Strictly below MAX_INNER_OPERATION_SECONDS in test_harness_timeout_budgets.py
# -- the inner-operation ceiling, not the lane's LANE_HANG_DETECTOR_SECONDS.
PROBE_TIMEOUT_SECONDS = 20


def _scripts_lane_modules() -> tuple[Path, ...]:
    return tuple(path for path in SCRIPT_TEST_PATHS if path.parent == SCRIPT_ROOT)


def _modules_reaching_the_credential_resolver() -> tuple[Path, ...]:
    """Lane modules importing something able to resolve a real credential."""
    reaching: list[Path] = []
    for path in _scripts_lane_modules():
        source = path.read_text(encoding="utf-8")
        if "import blueprint" in source or "from blueprint" in source:
            reaching.append(path)
    return tuple(reaching)


def _poisoned_home(tmp_path: Path) -> Path:
    """A throwaway home holding a stand-in "ambient" operator config.

    Manufacturing the ambient credential is what makes this guard non-vacuous
    on CI: the leak reproduces only when ``load_config()`` finds a config file,
    and a CI container has none.
    """
    home = tmp_path / "poisoned-home"
    home.mkdir()
    (home / ".blueprint-config.json").write_text(
        json.dumps(
            {
                "api_url": "https://ambient-operator-config.example",
                "api_key": AMBIENT_KEY,
                "default_repo": "ambient/repo",
                "default_base_branch": "main",
            }
        ),
        encoding="utf-8",
    )
    return home


def _load_scripts_conftest():
    """Import ``scripts/conftest.py`` under a private name.

    Loading it as ``conftest`` would collide with pytest's own conftest module
    registry, so it is imported as a plain module instead.
    """
    if str(SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(SCRIPT_ROOT))
    assert SCRIPTS_CONFTEST.is_file(), (
        f"{SCRIPTS_CONFTEST} is missing; the scripts test lane relies on its "
        "autouse fixture to keep ambient operator credentials out of unit tests"
    )
    spec = importlib.util.spec_from_file_location(
        "_scripts_lane_conftest", SCRIPTS_CONFTEST
    )
    assert spec is not None and spec.loader is not None, SCRIPTS_CONFTEST
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lane_module_scan_is_not_vacuous() -> None:
    """The scans below must actually see files.

    A guard that silently scans nothing and reports green is a recurring
    failure mode in this repo, so the counts are asserted before anything is
    concluded from them.
    """
    lane_modules = _scripts_lane_modules()
    assert len(lane_modules) >= 20, (
        "scripts/ lane discovery collapsed: expected the full test_*.py "
        f"corpus, found {len(lane_modules)}"
    )

    reaching = _modules_reaching_the_credential_resolver()
    assert len(reaching) >= 8, (
        "expected the credential-resolver scan to match the blueprint/"
        f"blueprint_api importers in scripts/, matched {len(reaching)}: "
        f"{sorted(path.name for path in reaching)}"
    )

    reaching_names = {path.name for path in reaching}
    for name in HISTORICALLY_LEAKING_MODULES:
        assert name in reaching_names, (
            f"{name} historically made live authenticated requests to the "
            "production orchestrator from this lane; it must stay inside the "
            "scanned set"
        )


def test_every_hermetic_credential_points_at_a_non_routable_host() -> None:
    """No layer may hand the lane a credential aimed at a resolvable host."""
    lane_host = HERMETIC_API_URL.split("://", 1)[-1].split("/", 1)[0]
    assert lane_host.endswith(NON_ROUTABLE_SUFFIX), (
        "backend/testing/test_scripts_test_lane.py HERMETIC_API_URL must point "
        f"at a {NON_ROUTABLE_SUFFIX} host, got {HERMETIC_API_URL!r}"
    )
    assert "bpsc_" not in HERMETIC_API_KEY, (
        "the hermetic key must not look like a real scoped ServiceCredential"
    )

    conftest = _load_scripts_conftest()
    conftest_host = conftest.HERMETIC_API_URL.split("://", 1)[-1].split("/", 1)[0]
    assert conftest_host.endswith(NON_ROUTABLE_SUFFIX), (
        "scripts/conftest.py HERMETIC_API_URL must point at a "
        f"{NON_ROUTABLE_SUFFIX} host, got {conftest.HERMETIC_API_URL!r}"
    )

    harness = SHELL_HARNESS.read_text(encoding="utf-8")
    expected = (
        "BLUEPRINT_HERMETIC_API_URL="
        f'"https://scripts-lane.blueprint{NON_ROUTABLE_SUFFIX}"'
    )
    assert expected in harness, (
        f"{SHELL_HARNESS} must export a {NON_ROUTABLE_SUFFIX} hermetic API URL"
    )


def test_scripts_conftest_installs_an_autouse_hermetic_fixture() -> None:
    """The directory-wide fixture must exist and apply without being requested."""
    assert SCRIPTS_CONFTEST.is_file(), (
        f"{SCRIPTS_CONFTEST} is the lane-wide guarantee for direct "
        "`pytest scripts/...` runs; a per-file fixture cannot cover test "
        "modules added later"
    )
    conftest = _load_scripts_conftest()
    fixture = getattr(conftest, "hermetic_blueprint_credentials", None)
    assert fixture is not None, (
        "scripts/conftest.py must define hermetic_blueprint_credentials"
    )
    marker = getattr(fixture, "_pytestfixturefunction", None)
    assert marker is not None and marker.autouse, (
        "hermetic_blueprint_credentials must be autouse: a fixture the lane has "
        "to opt into is exactly the per-file pattern this replaces"
    )


def test_scripts_conftest_defeats_an_ambient_operator_config(
    monkeypatch, tmp_path
) -> None:
    """Applying the fixture must neutralise every ``load_config()`` tier.

    Drives the real ``blueprint_api.load_config()`` against a manufactured
    ambient config, so it proves the behaviour rather than the presence of the
    code.
    """
    if str(SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(SCRIPT_ROOT))
    blueprint_api = importlib.import_module("blueprint_api")
    conftest = _load_scripts_conftest()

    home = _poisoned_home(tmp_path)
    for name in (
        "BLUEPRINT_CONFIG",
        "BLUEPRINT_API_URL",
        "BLUEPRINT_API_KEY",
        "ORCHESTRATOR_URL",
        "AGENT_SECRET_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(blueprint_api, "_config_cache", None)
    monkeypatch.setattr(
        blueprint_api,
        "_config_search_paths",
        lambda: [home / ".blueprint-config.json"],
    )

    # Unguarded, the ambient file is exactly what resolves -- the defect,
    # reproduced, so the assertion afterwards cannot pass vacuously.
    assert blueprint_api.load_config()["api_key"] == AMBIENT_KEY

    with pytest.MonkeyPatch.context() as lane:
        blueprint_api._config_cache = None
        conftest.hermetic_blueprint_credentials.__wrapped__(lane)
        resolved = blueprint_api.load_config()
        reachable = [Path(path) for path in blueprint_api._config_search_paths()]

    assert resolved["api_url"] == conftest.HERMETIC_API_URL
    assert resolved["api_key"] != AMBIENT_KEY

    # Defence in depth: BLUEPRINT_CONFIG alone is not enough. If the hermetic
    # file ever goes missing (a cleaned tmpdir, a stale cache) an unrestricted
    # search path falls straight through to the repo-root and home configs, so
    # the fixture must also narrow the search itself. Without this the resolver
    # can still SEE a real credential even though it currently prefers ours.
    assert reachable, "the hermetic fixture left the search path empty"
    escaped = [path for path in reachable if path != conftest.HERMETIC_CONFIG_PATH]
    assert not escaped, (
        "scripts/conftest.py must pin blueprint_api._config_search_paths() to "
        f"the hermetic config only; it can still reach {escaped}\n"
        + format_offender_marker(str(path) for path in escaped)
    )


def test_lane_subprocess_environment_defeats_an_ambient_operator_config(
    monkeypatch, tmp_path
) -> None:
    """The ``--noconftest`` CI path must be hermetic through its environment.

    ``test_scripts_test_lane.py`` runs every scripts test as a subprocess with
    ``--noconftest``, so ``scripts/conftest.py`` is skipped by construction and
    only the environment can carry the guarantee. Probe it exactly the way the
    lane builds it, against a manufactured ambient config.
    """
    # ``lane_subprocess_environment()`` starts from ``os.environ``, and merely
    # importing ``scripts/conftest.py`` (which the checks above do) installs the
    # hermetic variables into THIS process. Left in place they would satisfy the
    # assertion below no matter what the lane harness does -- the check would
    # pass with the injection deleted. Clear them so the only thing that can put
    # them back is the harness itself.
    for name in (
        "BLUEPRINT_CONFIG",
        "BLUEPRINT_API_URL",
        "BLUEPRINT_API_KEY",
        "ORCHESTRATOR_URL",
        "AGENT_SECRET_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    home = _poisoned_home(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json\n"
        "import blueprint_api\n"
        "print(json.dumps(blueprint_api.load_config()))\n",
        encoding="utf-8",
    )

    environment = lane_subprocess_environment()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)

    unguarded = {
        key: value
        for key, value in environment.items()
        if not key.startswith("BLUEPRINT_")
    }

    def _resolve(env: dict[str, str]) -> dict:
        result = subprocess.run(
            [sys.executable, str(probe)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            # Must stay strictly under MAX_INNER_OPERATION_SECONDS, the
            # inner-operation ceiling enforced by test_harness_timeout_budgets.py
            # -- NOT the lane's much larger LANE_HANG_DETECTOR_SECONDS. An inner
            # timeout at or above the outer budget means the harness kills a
            # healthy call before this one can fire, turning a real failure into
            # an unreproducible flake.
            # The probe only imports blueprint_api and resolves a config; it
            # runs in well under a second.
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    # Same probe, same lane PYTHONPATH, hermetic variables stripped: the
    # ambient operator config resolves. That is the defect, and it keeps the
    # assertion below from passing for the wrong reason.
    assert _resolve(unguarded)["api_key"] == AMBIENT_KEY

    resolved = _resolve(environment)
    assert resolved["api_url"] == HERMETIC_API_URL, (
        "a scripts-lane subprocess resolved a credential that is not the "
        "hermetic one; backend/testing/test_scripts_test_lane.py must keep "
        "injecting hermetic_credential_environment() into every lane run"
    )
    assert resolved["api_key"] == HERMETIC_API_KEY


def test_shell_harness_neutralises_ambient_credentials() -> None:
    """``scripts/test-backend.sh`` also runs the lane under ``--noconftest``."""
    harness = SHELL_HARNESS.read_text(encoding="utf-8")
    assert "--noconftest" in harness, (
        "this layer exists because the harness skips scripts/conftest.py; if "
        "--noconftest is gone, revisit whether the env layer is still needed"
    )
    assert 'ENV_VARS="$ENV_VARS BLUEPRINT_CONFIG= ' in harness, (
        f"{SHELL_HARNESS} must blank an inherited BLUEPRINT_CONFIG for the "
        "native (non-Docker) pytest path"
    )
    assert '-e "BLUEPRINT_CONFIG="' in harness, (
        f"{SHELL_HARNESS} must blank an inherited BLUEPRINT_CONFIG for the "
        "Docker pytest path"
    )
    for variable in ("BLUEPRINT_API_URL", "BLUEPRINT_API_KEY"):
        assert f"{variable}=$BLUEPRINT_HERMETIC_" in harness, (
            f"{SHELL_HARNESS} must export a hermetic {variable} for the native "
            "pytest path"
        )
        assert f'-e "{variable}=$BLUEPRINT_HERMETIC_' in harness, (
            f"{SHELL_HARNESS} must export a hermetic {variable} for the Docker "
            "pytest path"
        )


def test_leaking_modules_install_doubles_on_the_blueprint_api_binding() -> None:
    """Static backstop for the two modules whose doubles missed the binding.

    Both patched ``blueprint.<name>`` only, so ``blueprint_api``'s internal
    resolution ran for real. Require the ``blueprint_api`` binding -- the
    source of truth the imported names come from -- to be patched too.
    """
    for name in HISTORICALLY_LEAKING_MODULES:
        source = (SCRIPT_ROOT / name).read_text(encoding="utf-8")
        assert "import blueprint_api" in source, (
            f"scripts/{name} must import blueprint_api to install its doubles "
            "on the binding blueprint_api itself resolves"
        )
        assert "monkeypatch.setattr(blueprint_api, name, value)" in source, (
            f"scripts/{name} must install every double on the blueprint_api "
            "binding as well as the blueprint one; patching only blueprint is "
            "what let validate_project_id reach the production orchestrator"
        )
        for dual_bound in ("api", "load_config", "list_projects"):
            assert f'"{dual_bound}"' in source, (
                f"scripts/{name} should still install a double for "
                f"{dual_bound}"
            )


def test_dual_bound_names_are_still_imported_by_blueprint() -> None:
    """Pin the re-export list this whole guard reasons about.

    If ``blueprint.py`` stops importing these names from ``blueprint_api``, the
    two-namespace hazard has changed shape and the checks above need
    revisiting rather than silently continuing to pass.
    """
    source = (SCRIPT_ROOT / "blueprint.py").read_text(encoding="utf-8")
    start = source.find("from blueprint_api import (")
    assert start >= 0, "scripts/blueprint.py must import from blueprint_api"
    end = source.find(")", start)
    imported = source[start:end]
    for name in DUAL_BOUND_NAMES:
        assert name in imported, (
            f"{name} is no longer re-bound from blueprint_api into blueprint; "
            "revisit backend/testing/test_scripts_lane_hermetic_credentials.py"
        )
