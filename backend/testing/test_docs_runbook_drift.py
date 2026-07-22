"""Drift guard: docs/RUNNER_RUNBOOK.md stays in sync with RunnerPreset definitions.

``RunnerPreset`` in ``backend/app/routers/admin_runners.py`` is a ``Literal``
of valid preset names. This file ensures the runbook covers every preset and
contains no references to the (removed) resident subsystem — two failure modes
that are easy to miss during a docs-only PR.
"""

from __future__ import annotations

from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_ROOT = _THIS_DIR.parent
_REPO_ROOT = _BACKEND_ROOT.parent

_RUNBOOK_PATH = _REPO_ROOT / "docs" / "RUNNER_RUNBOOK.md"

# RunnerPreset = Literal["default", "strategic", "strategic_local"]
# in backend/app/routers/admin_runners.py:122
_PRESET_NAMES = ("default", "strategic", "strategic_local")


def test_all_preset_names_appear_in_runbook() -> None:
    """Every RunnerPreset name appears in the runbook."""
    text = _RUNBOOK_PATH.read_text()
    for name in _PRESET_NAMES:
        assert f'"{name}"' in text, (
            f'RunnerPreset "{name}" not found in docs/RUNNER_RUNBOOK.md'
        )


def test_no_resident_references() -> None:
    """The runbook must not reference the excised resident subsystem."""
    text = _RUNBOOK_PATH.read_text().lower()
    assert "resident" not in text, (
        "docs/RUNNER_RUNBOOK.md still references 'resident' "
        "(strategic-resident subsystem was removed)"
    )
