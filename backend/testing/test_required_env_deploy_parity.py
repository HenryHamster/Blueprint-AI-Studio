"""Guard Cloud Run deploy bindings against ``required_runtime_env`` drift.

The manifest is intentionally machine-readable (see its module docstring), so
deployment paths can prove every Cloud Run boot prerequisite is supplied before
a revision starts. ``setup.sh`` is outside the backend Docker test context, so
its mirror check skips cleanly when it is unavailable there.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from app.required_runtime_env import REQUIRED_RUNTIME_ENV

SCRATCH_ENV = "MIGRATION_APPLY_SCRATCH_DATABASE_URL"
ENCRYPTION_ENV = "GITHUB_PAT_ENCRYPTION_KEY"


def _binding_names(argument: str) -> set[str]:
    """Return environment names from a comma-separated gcloud binding list."""
    return {binding.split("=", 1)[0] for binding in argument.split(",") if binding}


def _required_cloud_run_names() -> set[str]:
    return {req.name for req in REQUIRED_RUNTIME_ENV if req.scope == "cloud_run"}


def _repo_file(name: str) -> Path:
    candidates = (Path(__file__).parents[2] / name, Path("/repo") / name)
    return next(path for path in candidates if path.exists())


def _cloudbuild_deploy_bindings() -> tuple[set[str], set[str]]:
    config_path = Path(__file__).parents[1] / "cloudbuild.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
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
    text = setup_path.read_text(encoding="utf-8")
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
    assert SCRATCH_ENV not in env_names | secret_names
    assert ENCRYPTION_ENV in secret_names


def test_setup_deploy_mirror_supplies_every_cloud_run_required_env():
    setup_path = _repo_file("setup.sh")

    env_names, secret_names = _setup_deploy_bindings(setup_path)
    missing = _required_cloud_run_names() - env_names - secret_names
    assert not missing, (
        "setup.sh's replace-all deploy bindings omit required Cloud Run env "
        f"variables: {sorted(missing)}"
    )
    assert SCRATCH_ENV not in env_names | secret_names
    assert ENCRYPTION_ENV in secret_names


def test_setup_provisions_real_encryption_key_without_scratch_placeholder():
    setup_path = _repo_file("setup.sh")

    setup = setup_path.read_text(encoding="utf-8")
    assert "ensure_fernet_encryption_key" in setup
    assert f'prompt_secret_or_skip "{ENCRYPTION_ENV}"' not in setup
    assert f'prompt_secret_or_skip "{SCRATCH_ENV}"' not in setup


def test_optional_scratch_binding_is_conditional_and_additive():
    setup_path = _repo_file("setup.sh")
    cloudbuild_path = Path(__file__).parents[1] / "cloudbuild.yaml"

    for source in (
        setup_path.read_text(encoding="utf-8"),
        cloudbuild_path.read_text(encoding="utf-8"),
    ):
        assert f"gcloud secrets describe {SCRATCH_ENV}" in source
        assert f"--update-secrets={SCRATCH_ENV}={SCRATCH_ENV}:latest" in source or (
            f'--update-secrets="{SCRATCH_ENV}={SCRATCH_ENV}:latest"' in source
        )


def test_setup_binds_encryption_key_to_existing_and_new_migration_jobs():
    setup_path = _repo_file("setup.sh")
    setup = setup_path.read_text(encoding="utf-8")

    existing_job = re.search(
        r"if gcloud run jobs describe blueprint-migrate(?P<body>.*?)\nelse",
        setup,
        flags=re.DOTALL,
    )
    new_job = re.search(
        r"else\n(?P<body>\s+gcloud run jobs create blueprint-migrate.*?)\nfi",
        setup,
        flags=re.DOTALL,
    )
    assert (
        existing_job
        and f"{ENCRYPTION_ENV}={ENCRYPTION_ENV}:latest" in existing_job.group("body")
    )
    assert new_job and f"{ENCRYPTION_ENV}={ENCRYPTION_ENV}:latest" in new_job.group(
        "body"
    )
