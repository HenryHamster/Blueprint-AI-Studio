"""Tests for the always-on derived-artifact freshness lane.

The lane exists because stale checked-in guard data is self-concealing: the
attributed static corpus charges it to exactly the one PR that introduces it and
warn-passes it as inherited on every PR after, so anything that slips past
authoring time surfaces first at the automain->main promotion matrix (this is
what killed promotion PRs #17738 and #17764). These tests pin the four
properties that make the lane worth having:

* it selects sentinels from the SAME registry the merge gate uses, and costs
  nothing when no registered surface changed;
* a NEW drift blocks, and the block names the drifted sentinel AND its exact
  regeneration command (a block that only says "red" is what sent the last two
  promotions to a hand-written refresh PR);
* an INHERITED drift never blocks, but is reported as stale guard data on the
  base branch rather than silently swallowed;
* an unusable current or baseline result fails CLOSED.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sentinel_freshness as lane  # noqa: E402
from static_release_guard import SENTINEL_REPAIR_COMMANDS, JUnitReport  # noqa: E402

_EMITTER_SENTINEL = "backend/testing/test_websocket_emitter_inventory.py"
_EMITTER_SELECTOR = "testing/test_websocket_emitter_inventory.py"
_EMITTER_ROUTE = "testing/test_websocket_emitter_inventory.py::test_snapshot"


def _completed(returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["bash", "scripts/test-backend.sh"],
        returncode=returncode,
        stdout="",
        stderr="",
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: list[str],
    current: JUnitReport,
    baseline: JUnitReport | None,
    current_exit: int,
    baseline_exit: int = 0,
    baseline_raises: bool = False,
) -> dict[str, list]:
    """Drive the lane without booting the real pytest harness.

    Only the process boundaries are faked (which files changed, what the two
    harness runs reported). Selection, attribution and the block/report decision
    stay real, so a mutation to any of them shows up in these tests.
    """
    calls: dict[str, list] = {"corpus": [], "extracted": []}

    monkeypatch.setattr(lane, "_changed_files", lambda repo, base: list(changed))
    monkeypatch.setattr(lane, "_base_ref", lambda repo, base: f"origin/{base}")

    def _fake_extract(repo, ref, base_root):
        calls["extracted"].append(ref)
        # The real extraction materializes the base tree; ``_base_selectors``
        # keeps only selectors whose file exists there, so the fake must too.
        target = Path(base_root) / "backend" / _EMITTER_SELECTOR
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    monkeypatch.setattr(lane, "_extract_base", _fake_extract)

    def _fake_corpus(repo, junit, *, selectors=None):
        calls["corpus"].append((Path(repo).name, tuple(selectors or ())))
        return _completed(current_exit if len(calls["corpus"]) == 1 else baseline_exit)

    monkeypatch.setattr(lane, "_run_static_corpus", _fake_corpus)

    def _fake_parse(path, *, tree_root=None, label=""):
        if label == "current":
            return current
        if baseline_raises:
            raise RuntimeError("baseline junit is unreadable")
        assert baseline is not None
        return baseline

    monkeypatch.setattr(lane, "parse_junit_report", _fake_parse)
    return calls


def test_a_registered_surface_selects_its_drift_sentinels() -> None:
    """Selection comes from the shared registry, not a private path list."""

    selected = lane.select_sentinels(["backend/app/anything.py"], str(REPO_ROOT))

    assert _EMITTER_SENTINEL in selected
    assert (
        "backend/testing/test_operator_workbench_contracts.py"
        "::test_websocket_emitter_inventory_covers_current_emitters" in selected
    )


def test_an_unregistered_surface_selects_nothing_and_never_runs_the_harness(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lane is free on a diff that touches no registered sentinel surface."""

    calls = _install_fakes(
        monkeypatch,
        changed=["docs/README.md"],
        current=JUnitReport(),
        baseline=JUnitReport(),
        current_exit=0,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 0
    assert calls["corpus"] == []
    assert "no registered drift-sentinel surface changed" in capsys.readouterr().out


def test_a_fresh_sentinel_passes_without_extracting_a_base_tree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean current run needs no baseline, so it costs one harness run."""

    calls = _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(),
        baseline=JUnitReport(),
        current_exit=0,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 0
    assert len(calls["corpus"]) == 1
    assert calls["extracted"] == []
    assert "every registered sentinel for this diff is fresh" in capsys.readouterr().out


def test_the_current_run_is_scoped_to_the_selected_sentinels(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoping to the matched sentinels is what keeps this lane always-on."""

    calls = _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(),
        baseline=JUnitReport(),
        current_exit=0,
    )

    lane._run_lane(REPO_ROOT, "automain")

    (_tree, selectors) = calls["corpus"][0]
    assert _EMITTER_SELECTOR in selectors
    # Harness form, not registry form: the harness runs pytest with cwd=backend.
    assert _EMITTER_SENTINEL not in selectors
    # A whole-corpus run would carry the roster's DB-free members too.
    assert "testing/test_security.py" not in selectors


def test_new_drift_blocks_and_names_the_exact_regeneration_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Surface the reason: which artifact drifted and how to regenerate it."""

    _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(failures={_EMITTER_ROUTE: "inventory mismatch"}),
        baseline=JUnitReport(),
        current_exit=1,
        baseline_exit=0,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 1
    err = capsys.readouterr().err
    assert "this change leaves derived guard data stale" in err
    assert _EMITTER_SENTINEL in err
    assert SENTINEL_REPAIR_COMMANDS[_EMITTER_SENTINEL] in err


def test_inherited_drift_does_not_block_but_is_reported_against_the_base_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inherited drift must never stop the fleet, and must never go unsaid.

    This is the state the promotion matrix converts into a fail-closed, so the
    lane names it, names the base ref it is stale on, and names the repair --
    while still returning 0, because the task that observed it did not cause it.
    """

    _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(failures={_EMITTER_ROUTE: "inventory mismatch"}),
        baseline=JUnitReport(failures={_EMITTER_ROUTE: "inventory mismatch"}),
        current_exit=1,
        baseline_exit=1,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 0
    err = capsys.readouterr().err
    assert "BASE GUARD DATA STALE on origin/automain" in err
    assert "WILL fail-close the next promotion PR" in err
    assert SENTINEL_REPAIR_COMMANDS[_EMITTER_SENTINEL] in err


def test_an_unattributable_current_run_fails_closed(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 with an empty report is a broken run, not a clean one."""

    _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(),
        baseline=JUnitReport(),
        current_exit=2,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 1


def test_an_unreadable_baseline_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a baseline the lane cannot excuse a failure, so it must not."""

    _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(failures={_EMITTER_ROUTE: "inventory mismatch"}),
        baseline=None,
        current_exit=1,
        baseline_raises=True,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 1
    assert "cannot read the baseline sentinel run" in capsys.readouterr().err


def test_an_unresolvable_base_ref_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clone that cannot see the base must not report a clean lane."""

    _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(),
        baseline=JUnitReport(),
        current_exit=0,
    )
    monkeypatch.setattr(lane, "_base_ref", lambda repo, base: None)

    assert lane._run_lane(REPO_ROOT, "automain") == 2
    assert "refusing to report clean without a baseline" in capsys.readouterr().err


def test_the_lane_reports_drift_and_never_repairs_it() -> None:
    """A guard that regenerates its own evidence is not a guard.

    The repair command is printed for a human to run in a reviewable diff; the
    lane itself must never invoke a regenerator.
    """

    source = (REPO_ROOT / "scripts" / "sentinel_freshness.py").read_text(
        encoding="utf-8"
    )

    assert "--fix" not in source
    assert "--write" not in source


def test_the_developer_gate_runs_this_lane_on_the_lint_only_path() -> None:
    """``./check`` with no ``test`` argument must still enforce freshness.

    The developer host has no ``./pr open``: ``scripts/check-default.sh`` is the
    only in-repo publication gate it has, and before this lane its always-on
    portion covered exactly one derived artifact (the size cap ledger).
    """

    gate = (REPO_ROOT / "scripts" / "check-default.sh").read_text(encoding="utf-8")

    assert "python3 scripts/sentinel_freshness.py" in gate
    invocation = gate.index("python3 scripts/sentinel_freshness.py")
    guard = gate.rindex('if [ "$WITH_TESTS" != "1" ]; then', 0, invocation)
    # The lane must sit under the NOT-with-tests guard: under ``./check test``
    # the attributed corpus already runs the whole roster.
    assert 'if [ "$WITH_TESTS" = "1" ]; then' not in gate[guard:invocation]
    # And its absence must fail closed rather than silently skip the gate.
    assert 'FAILED+=("sentinel freshness helper missing")' in gate
