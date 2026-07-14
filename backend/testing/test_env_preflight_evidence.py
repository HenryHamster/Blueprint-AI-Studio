"""Tests for the candidate-side env-preflight evidence builder (Slice 2).

Covers:
- build_env_preflight_evidence() emits the exact pure-content shape;
- required_env reflects REQUIRED_RUNTIME_ENV (name + scope, no value);
- manifest_source is the constant pointer to the manifest file;
- startup_check_names derive from smoketest.yaml suites.startup.commands;
- a synthetic ADDED EnvRequirement surfaces in the output (the v25 regression:
  a candidate that adds a boot prerequisite reports it before promotion);
- sanitize_env_preflight_artifact() narrows a raw agent blob to presence-only
  and drops any smuggled value / unexpected key.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.env_preflight_evidence import (
    MANIFEST_SOURCE,
    build_env_preflight_evidence,
    sanitize_env_preflight_artifact,
)
from app.required_runtime_env import REQUIRED_RUNTIME_ENV, EnvRequirement


# --------------------------------------------------------------------------- #
# build_env_preflight_evidence
# --------------------------------------------------------------------------- #
def test_build_emits_exact_shape():
    art = build_env_preflight_evidence()
    assert set(art.keys()) == {"required_env", "startup_check_names", "manifest_source"}
    assert art["manifest_source"] == MANIFEST_SOURCE == "backend/app/required_runtime_env.py"
    assert isinstance(art["required_env"], list)
    assert isinstance(art["startup_check_names"], list)


def test_required_env_reflects_manifest_name_and_scope_only():
    art = build_env_preflight_evidence()
    expected = [{"name": r.name, "scope": r.scope} for r in REQUIRED_RUNTIME_ENV]
    assert art["required_env"] == expected
    # Presence-only: entries carry ONLY name + scope — never a value/bool/reason.
    for entry in art["required_env"]:
        assert set(entry.keys()) == {"name", "scope"}


def test_startup_check_names_from_smoketest_yaml(tmp_path: Path):
    yaml_path = tmp_path / "smoketest.yaml"
    yaml_path.write_text(
        "suites:\n"
        "  startup:\n"
        "    commands:\n"
        '      - "python3 -c \\"import app.main\\""\n'
        '      - "python3 -c \\"import app.github\\""\n',
        encoding="utf-8",
    )
    art = build_env_preflight_evidence(smoketest_yaml_path=yaml_path)
    assert art["startup_check_names"] == [
        'python3 -c "import app.main"',
        'python3 -c "import app.github"',
    ]


def test_startup_check_names_empty_on_missing_yaml(tmp_path: Path):
    # Best-effort: an unreadable config yields [] rather than raising.
    art = build_env_preflight_evidence(smoketest_yaml_path=tmp_path / "nope.yaml")
    assert art["startup_check_names"] == []


def test_default_yaml_path_reads_repo_smoketest():
    # Against the real in-repo smoketest.yaml, the startup commands are surfaced.
    art = build_env_preflight_evidence()
    assert any("app.main" in cmd for cmd in art["startup_check_names"])


def test_added_requirement_surfaces(monkeypatch):
    # v25 regression: a candidate that ADDS a boot prerequisite must report it.
    added = EnvRequirement(name="NEW_BOOT_KEY", scope="always", reason="x")
    # build_env_preflight_evidence imports REQUIRED_RUNTIME_ENV at call time, so
    # patching the source module makes the added prerequisite visible.
    monkeypatch.setattr(
        "app.required_runtime_env.REQUIRED_RUNTIME_ENV",
        [*REQUIRED_RUNTIME_ENV, added],
    )
    art = build_env_preflight_evidence()
    assert {"name": "NEW_BOOT_KEY", "scope": "always"} in art["required_env"]


def test_module_main_prints_json():
    # The `python3 -m app.env_preflight_evidence` invocation the agent uses emits
    # a single parseable JSON line matching the builder output.
    proc = subprocess.run(
        [sys.executable, "-m", "app.env_preflight_evidence"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout.strip())
    assert parsed == build_env_preflight_evidence()


# --------------------------------------------------------------------------- #
# sanitize_env_preflight_artifact
# --------------------------------------------------------------------------- #
def test_sanitize_narrows_to_presence_only():
    raw = {
        "required_env": [
            {"name": "A", "scope": "always", "value": "SECRET", "present": True},
            {"name": "B", "scope": "cloud_run"},
        ],
        "startup_check_names": ["chk1", "chk2", 3],
        "manifest_source": "backend/app/required_runtime_env.py",
        "unexpected": "dropped",
    }
    out = sanitize_env_preflight_artifact(raw)
    assert out == {
        "required_env": [
            {"name": "A", "scope": "always"},
            {"name": "B", "scope": "cloud_run"},
        ],
        "startup_check_names": ["chk1", "chk2", "3"],
        "manifest_source": "backend/app/required_runtime_env.py",
    }
    # No smuggled value survives.
    for entry in out["required_env"]:
        assert set(entry.keys()) == {"name", "scope"}


def test_sanitize_drops_malformed_entries():
    raw = {
        "required_env": ["not-a-dict", {"name": "A"}, {"scope": "always"}, {"name": "C", "scope": "always"}],
        "startup_check_names": "not-a-list",
    }
    out = sanitize_env_preflight_artifact(raw)
    assert out["required_env"] == [{"name": "C", "scope": "always"}]
    assert out["startup_check_names"] == []
    # Missing manifest_source falls back to the canonical constant.
    assert out["manifest_source"] == MANIFEST_SOURCE


def test_sanitize_returns_none_for_non_dict():
    assert sanitize_env_preflight_artifact(None) is None
    assert sanitize_env_preflight_artifact("x") is None
    assert sanitize_env_preflight_artifact([1, 2]) is None
