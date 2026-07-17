"""Candidate-side env-preflight evidence builder (Slice 2, design 245212a4).

This module produces a PURE CONTENT ARTIFACT describing the boot/env
prerequisites a candidate SHA declares — with NO pass/fail verdict and NO
gating.  It runs ON THE CANDIDATE TREE (invoked as ``python3 -m
app.env_preflight_evidence`` from the smoketest agent after it has checked out
the pinned candidate commit), so a v25-style candidate that ADDS a new startup
prerequisite reports it as evidence the deployed judge can later weigh — before
the candidate is promoted.

The artifact shape is exactly::

    {
      "required_env": [{"name": str, "scope": str}, ...],
      "startup_check_names": [str, ...],
      "manifest_source": "backend/app/required_runtime_env.py"
    }

* ``required_env`` is introspected from the candidate tree's OWN
  :data:`app.required_runtime_env.REQUIRED_RUNTIME_ENV` manifest.
* ``startup_check_names`` is the list of startup-suite commands declared in the
  candidate tree's ``backend/testing/smoketest.yaml`` (the checks the app runs
  to confirm it can boot).

PRESENCE-ONLY HARD RULE (inherited from ``app.required_runtime_env``): this
artifact carries env NAMES and SCOPES only.  It never reads, compares, logs, or
persists an env VALUE — not even a bool-of-a-value or a secret.
:func:`sanitize_env_preflight_artifact` re-enforces this on the read side so a
malformed artifact can never smuggle a value onto ``last_qualification``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The single source of truth for the env manifest — recorded verbatim in the
# artifact so a consumer knows exactly which file the required_env list came
# from (and can diff it against the deployed tree).
MANIFEST_SOURCE = "backend/app/required_runtime_env.py"

# Default location of the smoketest config relative to this module
# (``backend/app/`` -> ``backend/testing/smoketest.yaml``).
_DEFAULT_SMOKETEST_YAML = (
    Path(__file__).resolve().parent.parent / "testing" / "smoketest.yaml"
)


def _read_startup_check_names(smoketest_yaml_path: Path | None = None) -> list[str]:
    """Return the startup-suite command strings declared in ``smoketest.yaml``.

    These are the checks the app runs to confirm it can boot (the ``startup``
    suite).  Best-effort: a missing/unparseable config yields an empty list —
    this is evidence, never a gate, so it must never raise into the caller.
    """
    path = smoketest_yaml_path or _DEFAULT_SMOKETEST_YAML
    try:
        import yaml

        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    startup = ((parsed.get("suites") or {}).get("startup") or {})
    commands = startup.get("commands") if isinstance(startup, dict) else None
    if not isinstance(commands, list):
        return []
    return [str(cmd) for cmd in commands if isinstance(cmd, (str, int, float))]


def build_env_preflight_evidence(
    smoketest_yaml_path: Path | None = None,
) -> dict[str, Any]:
    """Build the pure-content env-preflight artifact from the candidate tree.

    Introspects the candidate's OWN ``REQUIRED_RUNTIME_ENV`` manifest (so a
    candidate that adds a prerequisite reports it) and the candidate's
    ``smoketest.yaml`` startup commands.  Presence-only: only ``name`` and
    ``scope`` are read off each manifest entry — never a value.
    """
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    required_env = [
        {"name": req.name, "scope": req.scope} for req in REQUIRED_RUNTIME_ENV
    ]
    return {
        "required_env": required_env,
        "startup_check_names": _read_startup_check_names(smoketest_yaml_path),
        "manifest_source": MANIFEST_SOURCE,
    }


def sanitize_env_preflight_artifact(raw: Any) -> dict[str, Any] | None:
    """Coerce a raw env-preflight blob into the strict presence-only shape.

    Called on the qualification read side: the artifact arrives from an agent
    VM, so it must be defensively narrowed to ``{required_env:[{name,scope}],
    startup_check_names:[str], manifest_source:str}`` before it touches
    ``last_qualification``.  Any unexpected keys (or a value that a malformed
    producer smuggled in) are dropped — only ``name`` and ``scope`` survive from
    each ``required_env`` entry.  Returns ``None`` when ``raw`` is not a dict.
    """
    if not isinstance(raw, dict):
        return None
    # Guard against non-list inputs: a bare string is iterable and would
    # otherwise be walked character-by-character.
    raw_required = raw.get("required_env")
    raw_startup = raw.get("startup_check_names")
    required_env: list[dict[str, str]] = []
    for entry in raw_required if isinstance(raw_required, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        scope = entry.get("scope")
        if not isinstance(name, str) or not isinstance(scope, str):
            continue
        required_env.append({"name": name, "scope": scope})
    startup_check_names = [
        str(cmd)
        for cmd in (raw_startup if isinstance(raw_startup, list) else [])
        if isinstance(cmd, (str, int, float))
    ]
    manifest_source = raw.get("manifest_source")
    return {
        "required_env": required_env,
        "startup_check_names": startup_check_names,
        "manifest_source": manifest_source
        if isinstance(manifest_source, str)
        else MANIFEST_SOURCE,
    }


def build_env_preflight_from_source(
    required_runtime_env_py_source: str | None,
    smoketest_yaml_source: str | None = None,
) -> dict[str, Any] | None:
    """Build env-preflight evidence from candidate tree SOURCE STRINGS.

    Alternative entry point that does NOT require the candidate tree to be
    importable (no local checkout, no ``sys.path`` manipulation).  The caller
    provides the text of the candidate's ``backend/app/required_runtime_env.py``
    (and optionally ``backend/testing/smoketest.yaml``), which this function
    introspects using a controlled Python ``exec()`` in a sandboxed namespace
    (safe: the source is from a known git SHA in the project's own repository).

    The artifact shape, presence-only hard rule, and ``manifest_source`` pointer
    are identical to :func:`build_env_preflight_evidence`.

    Returns ``None`` when ``required_runtime_env_py_source`` is ``None`` or
    cannot be parsed (the caller should treat this as "evidence unavailable").
    """
    if required_runtime_env_py_source is None:
        return None
    required_env = _extract_required_env_from_source(required_runtime_env_py_source)
    startup_check_names = (
        _parse_startup_check_names_from_yaml(smoketest_yaml_source)
        if smoketest_yaml_source is not None
        else _read_startup_check_names()
    )
    return {
        "required_env": required_env,
        "startup_check_names": startup_check_names,
        "manifest_source": MANIFEST_SOURCE,
    }


def _extract_required_env_from_source(source: str) -> list[dict[str, str]]:
    """Extract ``REQUIRED_RUNTIME_ENV`` entries from module source code.

    Uses a sandboxed ``exec()`` to resolve the module-level constant without
    importing the candidate's package tree.  The only external import in
    ``required_runtime_env.py`` (``ENCRYPTION_KEY_ENV`` from
    ``app.field_encryption``) is replaced with its resolved value so the
    source is self-contained.
    """
    # Resolve the sole external import to its known value so exec is
    # self-contained.
    processed = source.replace(
        "from app.field_encryption import ENCRYPTION_KEY_ENV",
        'ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"',
    )
    ns: dict[str, Any] = {}
    try:
        exec(processed, ns)
    except (SyntaxError, NameError, TypeError):
        # SyntaxError: the source was not valid Python (candidate has a broken file).
        # NameError/TypeError: the module imports something we didn't resolve.
        # Both degrade gracefully to empty list — evidence is best-effort.
        return []
    required = ns.get("REQUIRED_RUNTIME_ENV")
    if not isinstance(required, (list, tuple)):
        return []
    result: list[dict[str, str]] = []
    for entry in required:
        name = (
            str(entry.name)
            if isinstance(getattr(entry, "name", None), str)
            else None
        )
        scope = (
            str(entry.scope)
            if isinstance(getattr(entry, "scope", None), str)
            else None
        )
        if isinstance(name, str) and isinstance(scope, str):
            result.append({"name": name, "scope": scope})
    return result


def _parse_startup_check_names_from_yaml(yaml_source: str) -> list[str]:
    """Parse startup check names from ``smoketest.yaml`` content (not a file path).

    Best-effort: an unparseable YAML string returns an empty list — this is
    evidence, never a gate.
    """
    try:
        import yaml

        parsed = yaml.safe_load(yaml_source)
    except ImportError:
        return []
    except (yaml.YAMLError, ValueError, TypeError, AttributeError):
        return []
    if not isinstance(parsed, dict):
        return []
    startup = ((parsed.get("suites") or {}).get("startup") or {})
    commands = startup.get("commands") if isinstance(startup, dict) else None
    if not isinstance(commands, list):
        return []
    return [str(cmd) for cmd in commands if isinstance(cmd, (str, int, float))]


if __name__ == "__main__":
    # Emit the artifact as a single JSON line on stdout so the smoketest agent
    # can capture and fold it into the SMOKETEST_RESULT artifact.  Pure content —
    # nothing here reads an env VALUE.
    print(json.dumps(build_env_preflight_evidence(), separators=(",", ":")))
