"""Keep the local release runbook aligned with the shipped release scripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_ROOT.parent
if not (_REPO_ROOT / "docs").is_dir() and (Path("/repo") / "docs").is_dir():
    _REPO_ROOT = Path("/repo")
_SCRIPTS_ROOT = _REPO_ROOT / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

import release_runbook_contract as contract  # noqa: E402


def _runbook_with_contract(tmp_path: Path, replacement: dict[str, object]) -> Path:
    source = contract._RUNBOOK_PATH.read_text(encoding="utf-8")
    match = contract._CONTRACT_RE.search(source)
    assert match is not None
    payload = json.dumps(replacement, indent=2, sort_keys=True)
    text = source[: match.start("payload")] + payload + source[match.end("payload") :]
    path = tmp_path / "local-app-release.md"
    path.write_text(text, encoding="utf-8")
    return path


def _expected_contract() -> dict[str, object]:
    return contract.expected_contract()


def test_runbook_matches_release_script_contract() -> None:
    result = contract.validate_runbook_contract()

    assert result["entrypoints"]["bootstrap"]["flags"] == [
        "--image",
        "--rehearse",
        "--revision",
    ]
    assert result["entrypoints"]["release"]["flags"] == [
        "--expected-revision",
        "--image",
        "--rehearse",
    ]
    assert "live systemd privilege boundary" in result["preconditions"]


def test_runbook_does_not_instruct_operator_to_use_disabled_host_rehearsal() -> None:
    text = contract._RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "Pre-stop rehearsal (mandatory gate)" not in text
    assert "does not run a standalone pre-stop command" in text
    assert "do not\ninvoke either release entrypoint with `--rehearse`" in text
    assert "Never bypass the integrated" in text
    assert "check and never run candidate code" in text


def test_missing_script_entrypoint_is_reported(tmp_path: Path) -> None:
    drifted = _expected_contract()
    del drifted["entrypoints"]["bootstrap"]  # type: ignore[index]
    path = _runbook_with_contract(tmp_path, drifted)

    with pytest.raises(AssertionError, match="missing entrypoint: bootstrap"):
        contract.validate_runbook_contract(path)


def test_documented_flag_that_script_does_not_have_is_reported(tmp_path: Path) -> None:
    drifted = _expected_contract()
    drifted["entrypoints"]["release"]["flags"].append(  # type: ignore[index]
        "--removed-flag"
    )
    path = _runbook_with_contract(tmp_path, drifted)

    with pytest.raises(AssertionError, match="release extra flags"):
        contract.validate_runbook_contract(path)


def test_removed_pre_stop_precondition_is_reported(tmp_path: Path) -> None:
    drifted = _expected_contract()
    drifted["preconditions"].remove(  # type: ignore[index]
        "live systemd privilege boundary"
    )
    path = _runbook_with_contract(tmp_path, drifted)

    with pytest.raises(AssertionError, match="preconditions differ"):
        contract.validate_runbook_contract(path)
