"""Guard Cloud Run deploy bindings against ``required_runtime_env`` drift.

The manifest is intentionally machine-readable (see its module docstring), so
deployment paths can prove every Cloud Run boot prerequisite is supplied before
a revision starts. ``setup.sh`` is outside the backend Docker test context, so
its mirror check skips cleanly when it is unavailable there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from app.required_runtime_env import REQUIRED_RUNTIME_ENV


def _binding_names(argument: str) -> set[str]:
    """Return environment names from a comma-separated gcloud binding list."""
    return {binding.split("=", 1)[0] for binding in argument.split(",") if binding}


def _required_cloud_run_names() -> set[str]:
    return {req.name for req in REQUIRED_RUNTIME_ENV if req.scope == "cloud_run"}


def _cloudbuild_deploy_bindings() -> tuple[set[str], set[str]]:
    config_path = Path(__file__).parents[1] / "cloudbuild.yaml"
    config = yaml.safe_load(config_path.read_text())
    deploy_step = next(
        step
        for step in config["steps"]
        if step.get("entrypoint") == "gcloud"
        and step.get("args", [])[:3] == ["run", "deploy", "blueprint-orchestrator"]
    )
    args = deploy_step["args"]
    env_argument = args[args.index("--set-env-vars") + 1]
    secret_argument = args[args.index("--set-secrets") + 1]
    return _binding_names(env_argument), _binding_names(secret_argument)


def _setup_deploy_bindings(setup_path: Path) -> tuple[set[str], set[str]]:
    text = setup_path.read_text()
    deploy = re.search(
        r"gcloud run deploy blueprint-orchestrator \\\n(?P<body>.*?)(?:\n  --project=)",
        text,
        flags=re.DOTALL,
    )
    assert deploy, "setup.sh must contain the inline blueprint-orchestrator deploy"
    body = deploy.group("body")
    env = re.search(r'--set-env-vars="(?P<value>[^"]+)"', body)
    secrets = re.search(r'--set-secrets="(?P<value>[^"]+)"', body)
    assert env, "setup.sh deploy must supply --set-env-vars"
    assert secrets, "setup.sh deploy must supply --set-secrets"
    return _binding_names(env.group("value")), _binding_names(secrets.group("value"))


def test_cloudbuild_deploy_supplies_every_cloud_run_required_env():
    env_names, secret_names = _cloudbuild_deploy_bindings()
    missing = _required_cloud_run_names() - env_names - secret_names
    assert not missing, (
        "cloudbuild.yaml's replace-all deploy bindings omit required Cloud Run env "
        f"variables: {sorted(missing)}"
    )


def test_setup_deploy_mirror_supplies_every_cloud_run_required_env():
    setup_path = Path(__file__).parents[2] / "setup.sh"
    if not setup_path.exists():
        pytest.skip("setup.sh is not included in this backend test context")

    env_names, secret_names = _setup_deploy_bindings(setup_path)
    missing = _required_cloud_run_names() - env_names - secret_names
    assert not missing, (
        "setup.sh's replace-all deploy bindings omit required Cloud Run env "
        f"variables: {sorted(missing)}"
    )


def test_setup_creates_placeholder_secrets_for_cloud_run_requirements():
    setup_path = Path(__file__).parents[2] / "setup.sh"
    if not setup_path.exists():
        pytest.skip("setup.sh is not included in this backend test context")

    setup = setup_path.read_text()
    missing = {
        name
        for name in _required_cloud_run_names()
        if f'prompt_secret_or_skip "{name}"' not in setup
    }
    assert not missing, (
        "setup.sh must create placeholder secrets so gcloud can validate the "
        f"required deploy bindings: {sorted(missing)}"
    )
