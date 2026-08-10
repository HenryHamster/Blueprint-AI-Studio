"""Machine-readable contract between the local release scripts and runbook."""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPT_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_ROOT.parent
_RUNBOOK_PATH = _REPO_ROOT / "docs" / "operations" / "local-app-release.md"
_CONTRACT_RE = re.compile(
    r"<!-- RELEASE_RUNBOOK_CONTRACT:START -->\s*"
    r"```json\s*(?P<payload>.*?)\s*```\s*"
    r"<!-- RELEASE_RUNBOOK_CONTRACT:END -->",
    re.DOTALL,
)
_ENTRYPOINT_MODULES = (
    ("bootstrap", "bootstrap_local_app_release", "scripts/bootstrap_local_app_release.py"),
    ("release", "local_app_release", "scripts/local_app_release.py"),
)


def _parser_flags(parser: Any) -> list[str]:
    """Return user-facing options from an argparse parser, excluding help."""
    flags = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option not in {"-h", "--help"}
    }
    return sorted(flags)


def expected_contract() -> dict[str, Any]:
    """Build the script-side contract from the shipped parser definitions."""
    if str(_SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_ROOT))

    entrypoints: dict[str, Any] = {}
    for name, module_name, script_path in _ENTRYPOINT_MODULES:
        module = importlib.import_module(module_name)
        declared_name = getattr(module, "RUNBOOK_ENTRYPOINT", None)
        if declared_name != name:
            raise AssertionError(
                f"{module_name}.RUNBOOK_ENTRYPOINT={declared_name!r}; expected {name!r}"
            )
        build_parser = getattr(module, "build_parser", None)
        if build_parser is None:
            raise AssertionError(f"{module_name}.build_parser() is missing")
        entrypoints[name] = {
            "flags": _parser_flags(build_parser()),
            "script": script_path,
        }

    preconditions = importlib.import_module("release_preconditions")
    names = getattr(preconditions, "PRE_STOP_PRECONDITIONS", None)
    if not isinstance(names, tuple) or not names or not all(
        isinstance(name, str) and name for name in names
    ):
        raise AssertionError(
            "release_preconditions.PRE_STOP_PRECONDITIONS must be a non-empty tuple"
        )

    return {"entrypoints": entrypoints, "preconditions": list(names)}


def load_runbook_contract(path: Path = _RUNBOOK_PATH) -> dict[str, Any]:
    """Parse exactly one marked JSON contract from the runbook."""
    text = path.read_text(encoding="utf-8")
    matches = list(_CONTRACT_RE.finditer(text))
    if len(matches) != 1:
        raise AssertionError(
            f"{path} must contain exactly one RELEASE_RUNBOOK_CONTRACT block; "
            f"found {len(matches)}"
        )
    try:
        contract = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"invalid runbook contract JSON: {exc}") from exc
    if not isinstance(contract, dict):
        raise TypeError("runbook contract must be a JSON object")
    return contract


def validate_runbook_contract(path: Path = _RUNBOOK_PATH) -> dict[str, Any]:
    """Raise with actionable drift details when scripts and runbook disagree."""
    expected = expected_contract()
    actual = load_runbook_contract(path)
    if actual == expected:
        return expected

    differences: list[str] = []
    expected_entries = expected["entrypoints"]
    actual_entries = actual.get("entrypoints", {})
    if not isinstance(actual_entries, dict):
        differences.append("entrypoints is not an object")
        actual_entries = {}
    for name in sorted(set(expected_entries) | set(actual_entries)):
        if name not in actual_entries:
            differences.append(f"missing entrypoint: {name}")
        elif name not in expected_entries:
            differences.append(f"extra entrypoint: {name}")
        elif actual_entries[name] != expected_entries[name]:
            expected_flags = expected_entries[name].get("flags", [])
            actual_flags = actual_entries[name].get("flags", [])
            missing_flags = sorted(set(expected_flags) - set(actual_flags))
            extra_flags = sorted(set(actual_flags) - set(expected_flags))
            if missing_flags:
                differences.append(f"{name} missing flags: {missing_flags}")
            if extra_flags:
                differences.append(f"{name} extra flags: {extra_flags}")
            if actual_entries[name].get("script") != expected_entries[name]["script"]:
                differences.append(f"{name} script path does not match")

    actual_preconditions = actual.get("preconditions")
    if actual_preconditions != expected["preconditions"]:
        actual_names = actual_preconditions if isinstance(actual_preconditions, list) else []
        differences.append(
            "preconditions differ: "
            f"missing={sorted(set(expected['preconditions']) - set(actual_names))}, "
            f"extra={sorted(set(actual_names) - set(expected['preconditions']))}"
        )
    raise AssertionError("release runbook drift: " + "; ".join(differences))
