"""Hermetic credential defaults for the standalone ``scripts/test_*.py`` corpus.

Every module in this directory is a unit test, but several of them drive
``blueprint.main()`` across the real argparse/dispatch boundary. ``blueprint.py``
does ``from blueprint_api import (api, load_config, validate_project_id, ...)``,
which creates a SECOND binding for each name. A test double installed as
``monkeypatch.setattr(blueprint, "api", fake)`` rebinds only *blueprint's* copy:
``validate_project_id`` still resolves ``list_projects`` -> ``load_config`` ->
``api`` against the names in ``blueprint_api``, so those calls run the REAL
credential resolver.

``blueprint_api._config_search_paths()`` ends at the repo-root and
``~/.blueprint-config.json`` files, and ``_bootstrap_from_gcloud()`` will even
mint a fresh admin key out of Secret Manager and cache it to the home directory.
On a credentialed developer machine the lane therefore made live authenticated
HTTPS requests to the production orchestrator -- 24 of them across
``test_hermes_operator_cli.py`` and ``test_blueprint_fast_claim_repair_cli.py``
-- carrying the operator's admin key and printing the real project catalog into
pytest's captured stderr. CI containers have no config file anywhere in the
search order, so the lane is green there and can NEVER catch this.

This file closes the developer-machine half by neutralising every tier of
``load_config()`` for the whole directory, so no future test module has to
remember to do it and none can reach a real credential by forgetting which
module binding a name resolves in.

The CI-shaped runs pass ``--noconftest`` (``scripts/test-backend.sh`` and
``backend/testing/test_scripts_test_lane.py``), which by construction skips this
file; both of those harnesses inject the same guarantee through the subprocess
environment instead. ``backend/testing/test_scripts_lane_hermetic_credentials.py``
asserts all three layers stay in place and fails loudly if one is dropped.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# RFC 6761 reserves ``.invalid`` as guaranteed-not-resolvable, so even a request
# that escapes every mock in the lane cannot reach a real host. Any replacement
# sentinel must keep that suffix; the guard test pins it.
HERMETIC_API_URL = "https://scripts-lane.blueprint.invalid"
HERMETIC_API_KEY = "hermetic-scripts-lane-not-a-credential"
HERMETIC_DEFAULT_REPO = "hermetic/scripts-lane"

_HERMETIC_DIR = tempfile.mkdtemp(prefix="blueprint-scripts-lane-hermetic-")
atexit.register(shutil.rmtree, _HERMETIC_DIR, ignore_errors=True)

HERMETIC_CONFIG_PATH = Path(_HERMETIC_DIR) / "hermetic-blueprint-config.json"
HERMETIC_CONFIG_PATH.write_text(
    json.dumps(
        {
            "api_url": HERMETIC_API_URL,
            "api_key": HERMETIC_API_KEY,
            "default_repo": HERMETIC_DEFAULT_REPO,
            "default_base_branch": "main",
        }
    ),
    encoding="utf-8",
)


def hermetic_environment() -> dict[str, str]:
    """Env overrides that make ``load_config()`` host-independent.

    ``BLUEPRINT_CONFIG`` is tier 1 of ``load_config()`` and short-circuits the
    whole search, including the ambient repo-root/home files and the gcloud
    bootstrap. The ``BLUEPRINT_API_URL``/``BLUEPRINT_API_KEY`` pair is tier 2 and
    keeps the lane hermetic for the handful of tests that deliberately clear
    ``BLUEPRINT_CONFIG`` to exercise a lower tier.
    """
    return {
        "BLUEPRINT_CONFIG": str(HERMETIC_CONFIG_PATH),
        "BLUEPRINT_API_URL": HERMETIC_API_URL,
        "BLUEPRINT_API_KEY": HERMETIC_API_KEY,
    }


# Applied at import time as well as per-test: pytest imports conftest before it
# collects anything, so a module that resolves config at import/collection time
# is covered too, which a fixture alone cannot reach.
os.environ.update(hermetic_environment())


@pytest.fixture(autouse=True)
def hermetic_blueprint_credentials(monkeypatch):
    """Make ``blueprint_api.load_config()`` unable to reach a real credential.

    Re-applies the environment per test (so one test clearing a variable cannot
    leak into the next) and, when ``blueprint_api`` is already imported, closes
    the tiers an environment variable cannot reach:

    * ``_config_search_paths`` -> only the throwaway hermetic file, so a test
      that clears both credential env tiers still cannot fall through to the
      operator's repo-root or ``~/.blueprint-config.json``.
    * ``_bootstrap_from_gcloud`` -> ``None``, so no unit test shells out to
      ``gcloud secrets versions access`` and caches a production admin key into
      the developer's home directory.
    * ``_config_cache`` -> ``None``, so nothing resolved earlier survives.

    Tests that exercise ``load_config()``'s own search/precedence/error
    behaviour override these with their own ``monkeypatch`` calls, which run
    after this fixture and are undone independently at teardown.
    """
    for name, value in hermetic_environment().items():
        monkeypatch.setenv(name, value)

    blueprint_api = sys.modules.get("blueprint_api")
    if blueprint_api is not None:
        monkeypatch.setattr(
            blueprint_api, "_config_search_paths", lambda: [HERMETIC_CONFIG_PATH]
        )
        monkeypatch.setattr(blueprint_api, "_bootstrap_from_gcloud", lambda: None)
        monkeypatch.setattr(blueprint_api, "_config_cache", None)
