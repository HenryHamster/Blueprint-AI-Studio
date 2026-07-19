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


# --------------------------------------------------------------------------- #
# build_env_preflight_from_source — source-string entry point
# --------------------------------------------------------------------------- #
_REQUIRED_RUNTIME_SOURCE = """\
from __future__ import annotations

from dataclasses import dataclass

ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"


@dataclass(frozen=True)
class EnvRequirement:
    name: str
    scope: str
    presence_only: bool = True
    reason: str = ""


REQUIRED_RUNTIME_ENV: list[EnvRequirement] = [
    EnvRequirement(name=ENCRYPTION_KEY_ENV, scope="cloud_run", reason="x"),
    EnvRequirement(name="ALWAYS_VAR", scope="always", reason="y"),
]
"""

_SMOKETEST_YAML_SOURCE = """\
suites:
  startup:
    commands:
      - "python3 -c \\"import app.main\\""
      - "python3 -c \\"import app.github\\""
"""


def test_build_from_source_emits_exact_shape():
    """build_env_preflight_from_source with a valid Python source yields the
    same pure-content shape as build_env_preflight_evidence()."""
    from app.env_preflight_evidence import build_env_preflight_from_source

    art = build_env_preflight_from_source(
        required_runtime_env_py_source=_REQUIRED_RUNTIME_SOURCE,
        smoketest_yaml_source=_SMOKETEST_YAML_SOURCE,
    )
    assert art is not None
    assert set(art.keys()) == {"required_env", "startup_check_names", "manifest_source"}
    assert art["manifest_source"] == "backend/app/required_runtime_env.py"


def test_build_from_source_required_env_names_and_scope_only():
    """Entries carry only name + scope — never a value or reason."""
    from app.env_preflight_evidence import build_env_preflight_from_source

    art = build_env_preflight_from_source(
        required_runtime_env_py_source=_REQUIRED_RUNTIME_SOURCE,
    )
    assert art is not None
    assert {"name": "GITHUB_PAT_ENCRYPTION_KEY", "scope": "cloud_run"} in art["required_env"]
    assert {"name": "ALWAYS_VAR", "scope": "always"} in art["required_env"]
    for entry in art["required_env"]:
        assert set(entry.keys()) == {"name", "scope"}


def test_build_from_source_startup_check_names():
    """Startup check names are parsed from the optional smoketest YAML source."""
    from app.env_preflight_evidence import build_env_preflight_from_source

    art = build_env_preflight_from_source(
        required_runtime_env_py_source=_REQUIRED_RUNTIME_SOURCE,
        smoketest_yaml_source=_SMOKETEST_YAML_SOURCE,
    )
    assert art is not None
    assert art["startup_check_names"] == [
        'python3 -c "import app.main"',
        'python3 -c "import app.github"',
    ]


def test_build_from_source_returns_none_for_none_source():
    """None source produces None (evidence unavailable)."""
    from app.env_preflight_evidence import build_env_preflight_from_source

    assert build_env_preflight_from_source(None) is None
    assert build_env_preflight_from_source(None, smoketest_yaml_source="a: b") is None


def test_build_from_source_malformed_source_graceful():
    """A syntactically invalid required_runtime_env source returns [] (empty
    required_env) — the evidence builder degrades gracefully."""
    from app.env_preflight_evidence import build_env_preflight_from_source

    art = build_env_preflight_from_source(
        required_runtime_env_py_source="this is not valid python {{{",
    )
    assert art is not None
    assert art["required_env"] == []
    assert isinstance(art["startup_check_names"], list)
    assert art["manifest_source"] == "backend/app/required_runtime_env.py"


def test_build_from_source_missing_required_env_constant():
    """Source without a REQUIRED_RUNTIME_ENV constant yields an empty list."""
    from app.env_preflight_evidence import build_env_preflight_from_source

    art = build_env_preflight_from_source(
        required_runtime_env_py_source="x = 1\\ny = 2",
    )
    assert art is not None
    assert art["required_env"] == []


def test_build_from_source_default_yaml_fallback():
    """When smoketest_yaml_source is None and the file exists on disk, the
    function falls back to the real smoketest.yaml (same as
    build_env_preflight_evidence)."""
    from app.env_preflight_evidence import build_env_preflight_from_source

    art = build_env_preflight_from_source(
        required_runtime_env_py_source=_REQUIRED_RUNTIME_SOURCE,
    )
    assert art is not None
    # The default smoketest.yaml on disk has startup commands referencing app.main.
    assert any("app.main" in cmd for cmd in art["startup_check_names"])
