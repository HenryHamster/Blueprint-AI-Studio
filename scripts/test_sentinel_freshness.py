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

Since ``--fix`` there is a fifth: a drifted artifact that carries no decision is
REGENERATED and then re-verified, while every artifact that does carry one (a
ratchet, a floor, a snapshot, a cap, a measured duration) is still only
reported. Reporting alone was what cost three hand-written repair commits in
three days, each cut after a promotion PR had already gone red.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sentinel_freshness as lane  # noqa: E402
from derived_artifact_repair import (  # noqa: E402
    AUTOREPAIRABLE,
    MEASUREMENT_TAX_GUIDANCE,
    NEVER_AUTOREPAIR,
    ROT_ONLY_AUTOREPAIR,
    RepairOutcome,
)
from static_release_guard import SENTINEL_REPAIR_COMMANDS, JUnitReport  # noqa: E402

_EMITTER_SENTINEL = "backend/testing/test_websocket_emitter_inventory.py"
_EMITTER_SELECTOR = "testing/test_websocket_emitter_inventory.py"
_EMITTER_ROUTE = "testing/test_websocket_emitter_inventory.py::test_snapshot"

# The one artifact registered as decision-free: a pure report with no consumer
# but its own guard. Pinned as a literal so a rename of the registry entry
# cannot quietly move which artifact this lane is allowed to rewrite.
_QUEUE_SENTINEL = "backend/testing/test_test_estate_ranked_queue.py"
_QUEUE_ROUTE = (
    "testing/test_test_estate_ranked_queue.py"
    "::test_checked_in_queue_ranks_every_collected_test_file"
)


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
    repaired: JUnitReport | None = None,
    repaired_exit: int = 0,
    repair_outcome: RepairOutcome | None = None,
) -> dict[str, list]:
    """Drive the lane without booting the real pytest harness.

    Only the process boundaries are faked (which files changed, what the two
    harness runs reported). Selection, attribution and the block/report decision
    stay real, so a mutation to any of them shows up in these tests.
    """
    calls: dict[str, list] = {
        "corpus": [],
        "extracted": [],
        "repaired": [],
        "registries": [],
    }

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

    def _fake_repair(repo, sentinels, registry=None):
        # ``registry`` narrows which generators a pass may execute; recording it
        # keeps the rot-only pass from being indistinguishable from the ordinary
        # one in the call log.
        selected = tuple(sorted(sentinels))
        calls["repaired"].append(selected)
        calls["registries"].append(registry)
        if repair_outcome is not None:
            return repair_outcome
        return RepairOutcome(repaired=selected)

    monkeypatch.setattr(lane, "repair_sentinels", _fake_repair)

    def _phase() -> str:
        """Which of the up-to-three harness runs this call is.

        Order is current -> post-repair re-run (only when a repair ran) ->
        base. Deriving it from the call log rather than from the parser's
        ``label`` keeps the fakes honest about a repair that never happened.
        """
        if not calls["corpus"]:
            return "current"
        if calls["repaired"] and len(calls["corpus"]) == 1:
            return "repaired"
        return "base"

    def _fake_corpus(repo, junit, *, selectors=None):
        phase = _phase()
        calls["corpus"].append((Path(repo).name, tuple(selectors or ()), phase))
        return _completed(
            {
                "current": current_exit,
                "repaired": repaired_exit,
                "base": baseline_exit,
            }[phase]
        )

    monkeypatch.setattr(lane, "_run_static_corpus", _fake_corpus)

    def _fake_parse(path, *, tree_root=None, label=""):
        phase = calls["corpus"][-1][2]
        if phase == "current":
            return current
        if phase == "repaired":
            assert repaired is not None
            return repaired
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

    (_tree, selectors, _phase) = calls["corpus"][0]
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


def test_repair_is_opt_in_and_never_runs_without_fix(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--fix`` the lane is exactly what it was: report only.

    Replaces an earlier source-text assertion that the strings ``--fix`` and
    ``--write`` appeared nowhere in this module. That form could only ever
    describe the default path, and any rename defeated it. This asserts the
    behaviour instead: the regenerator is not merely un-named, it is not called.
    """

    calls = _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_new_thing.py"],
        current=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        baseline=JUnitReport(),
        current_exit=1,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 1
    assert calls["repaired"] == []


def test_only_decision_free_artifacts_are_registered_as_auto_repairable() -> None:
    """A guard that regenerates its own evidence is not a guard.

    That rule still holds for every artifact that encodes a DECISION -- a
    ratchet, a floor, a snapshot, a cap -- and for every MEASUREMENT no
    checkout can reproduce. The single admitted exception is a pure report:
    derived from the tree, read by no consumer but its own guard, so
    regenerating it discards no review signal.

    Each exclusion is pinned by literal name. If a later change wants one of
    these regenerated automatically, that is a decision to argue for in review,
    not a line to delete quietly.
    """

    assert set(AUTOREPAIRABLE) == {_QUEUE_SENTINEL}

    for sentinel in (
        "backend/testing/test_test_durations_freshness.py",
        "backend/testing/test_vitest_durations_freshness.py",
        "backend/testing/test_conversational_workday_protection.py",
        "backend/testing/test_event_type_invariants.py",
        "backend/testing/test_websocket_emitter_inventory.py",
        "backend/testing/test_operator_workbench_contracts.py",
    ):
        assert sentinel not in AUTOREPAIRABLE
        assert NEVER_AUTOREPAIR.get(sentinel), (
            f"{sentinel} is excluded from auto-repair but records no reason; "
            "an unexplained exclusion is one nobody can review"
        )

    # Every auto-repairable sentinel must ALSO keep its human-facing repair
    # hint, so a failure that survives the repair still tells a person what to
    # run, and the two registries cannot describe different commands.
    for sentinel, entry in AUTOREPAIRABLE.items():
        hint = SENTINEL_REPAIR_COMMANDS[sentinel]
        assert entry.script in hint
        for argument in entry.args:
            assert argument in hint


def test_a_ratchet_sentinel_is_reported_and_never_regenerated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--fix`` must not become a blanket regenerate-everything switch."""

    calls = _install_fakes(
        monkeypatch,
        changed=["backend/app/anything.py"],
        current=JUnitReport(failures={_EMITTER_ROUTE: "inventory mismatch"}),
        baseline=JUnitReport(),
        current_exit=1,
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 1
    # The emitter sentinel is a reviewed snapshot: no repair is attempted at
    # all, and the block still names the hand command.
    assert calls["repaired"] == []
    assert SENTINEL_REPAIR_COMMANDS[_EMITTER_SENTINEL] in capsys.readouterr().err


def test_new_drift_in_a_pure_report_is_regenerated_and_reverified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #17936 shape: a test file added without regenerating the queue."""

    calls = _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_filter_malformed_payload.py"],
        current=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        baseline=JUnitReport(),
        current_exit=1,
        repaired=JUnitReport(),
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 0
    assert calls["repaired"] == [(_QUEUE_SENTINEL,)]
    # Re-verified, not asserted: the repair is followed by a real re-run of the
    # sentinel it claims to have healed.
    assert [entry[2] for entry in calls["corpus"]] == ["current", "repaired"]
    assert calls["corpus"][1][1] == ("testing/test_test_estate_ranked_queue.py",)
    output = capsys.readouterr()
    assert "SELF-HEALED" in output.err
    assert "test-estate-ranked-queue.md" in output.out + output.err


def test_inherited_drift_in_a_pure_report_is_healed_not_merely_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fleet-level property, and the reason this fix is structural.

    Inherited drift warn-passes, so before ``--fix`` nothing on any branch ever
    repaired the integration branch and the promotion matrix was the first
    thing to fail-close on it. Healing happens before attribution, so this case
    needs no baseline at all: the repair rides out on whichever PR next touches
    the surface.
    """

    calls = _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_new_thing.py"],
        current=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        baseline=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        current_exit=1,
        baseline_exit=1,
        repaired=JUnitReport(),
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 0
    assert calls["extracted"] == [], (
        "a healed sentinel must not pay for a base tree just to learn whose "
        "drift it was"
    )
    assert "SELF-HEALED" in capsys.readouterr().err


def test_a_repair_that_does_not_clear_the_drift_still_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Never take the regenerator's word for it.

    A generator can exit 0 and leave the guard failing -- the failure was not
    staleness. Crediting the repair on its exit code alone would convert this
    guard into one that cannot fail.
    """

    _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_new_thing.py"],
        current=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        baseline=JUnitReport(),
        current_exit=1,
        repaired=JUnitReport(failures={_QUEUE_ROUTE: "still 1181 ranked"}),
        repaired_exit=1,
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 1
    assert "this change leaves derived guard data stale" in capsys.readouterr().err


def test_a_generator_that_cannot_run_fails_the_lane_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrunnable regenerator must never be reported as a repair."""

    _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_new_thing.py"],
        current=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        baseline=JUnitReport(),
        current_exit=1,
        repair_outcome=RepairOutcome(failed={_QUEUE_SENTINEL: "exited 2"}),
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 1
    assert "a registered generator could not be run" in capsys.readouterr().err


def test_an_unattributable_post_repair_run_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-run that proves nothing must not be allowed to clear a failure."""

    _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_new_thing.py"],
        current=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        baseline=JUnitReport(),
        current_exit=1,
        repaired=JUnitReport(),
        repaired_exit=2,
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 1
    assert "post-repair sentinel run is not attributable" in capsys.readouterr().err


def test_healing_one_sentinel_never_clears_another(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mixed failure must keep the un-repairable half blocking."""

    _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_new_thing.py", "backend/app/anything.py"],
        current=JUnitReport(
            failures={
                _QUEUE_ROUTE: "1181 ranked, 1182 collected",
                _EMITTER_ROUTE: "inventory mismatch",
            }
        ),
        baseline=JUnitReport(),
        current_exit=1,
        repaired=JUnitReport(),
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 1
    err = capsys.readouterr().err
    assert "SELF-HEALED" in err
    assert _EMITTER_ROUTE in err


def test_a_measurement_sentinel_is_told_what_NOT_to_do_instead(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The new-file tax needs different words from ordinary drift.

    Adding a test file leaves the durations artifacts with no entry for it, and
    the honest repair is a 15-20 minute timed run nobody can do from a task VM.
    A bare "regenerate it" then reads as an instruction to produce the artifact
    by any means -- and the two cheapest means are deleting the new test (test
    erosion is one of the two weights this repo scores releases on) or
    hand-writing a duration (which marks the file covered forever so nothing
    ever measures it). Both were observed; the block says so.
    """

    durations = "backend/testing/test_test_durations_freshness.py"
    coverage_failure = {
        "testing/test_test_durations_freshness.py"
        "::test_durations_coverage_fraction_within_threshold": (
            "introduced 1 uncovered test file(s)"
        )
    }
    _install_fakes(
        monkeypatch,
        changed=["backend/.test_durations"],
        current=JUnitReport(failures=dict(coverage_failure)),
        baseline=JUnitReport(),
        current_exit=1,
        # The rot-only pruner runs and finds NOTHING to delete: a coverage
        # shortfall is the opposite direction from rot. It must not be able to
        # convert that into a heal, so the post-prune re-run still fails.
        repair_outcome=RepairOutcome(already_fresh=(durations,)),
        repaired=JUnitReport(failures=dict(coverage_failure)),
        repaired_exit=1,
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 1
    err = capsys.readouterr().err
    assert "ADDITION, NOT ROT" in err, (
        "an addition on a measurement artifact must be named as such; "
        "'regenerate it' and 'real defect' both send the reader the wrong way"
    )
    assert "SELF-HEALED" not in err
    assert MEASUREMENT_TAX_GUIDANCE[durations] in err
    assert "test erosion" in err


def test_rot_on_a_measurement_artifact_is_healed_by_the_narrowed_registry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the asymmetry: stale rows ARE repairable offline.

    A row for a test that no longer exists needs no measurement -- deleting it
    is exactly what a full regeneration would do to that row. Charging a 15-20
    minute timed run for it is what leaves rot sitting on the integration
    branch until the promotion PR fail-closes on it, and a red promotion run
    closes the PR and deletes its release branch.

    Two things are pinned here. The prune must clear the block, and it must run
    under ``ROT_ONLY_AUTOREPAIR`` -- passing the narrowed registry is what stops
    this path from also being able to invoke the full regeneration command that
    the same sentinel is registered as NEVER auto-repairable for.
    """

    durations = "backend/testing/test_test_durations_freshness.py"
    calls = _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_removed_thing.py"],
        current=JUnitReport(
            failures={
                "testing/test_test_durations_freshness.py"
                "::test_durations_no_stale_entries": (
                    "1 stale entry(ies) in backend/.test_durations"
                )
            }
        ),
        baseline=JUnitReport(),
        current_exit=1,
        repair_outcome=RepairOutcome(repaired=(durations,)),
        repaired=JUnitReport(),
        repaired_exit=0,
    )

    assert lane._run_lane(REPO_ROOT, "automain", fix=True) == 0
    err = capsys.readouterr().err
    assert "SELF-HEALED (rot only)" in err
    assert "pruned stale entries from backend/.test_durations" in err
    assert calls["registries"] == [ROT_ONLY_AUTOREPAIR], (
        "the rot-only pass must pass its narrowed registry; with the default "
        "registry this sentinel would be skipped, and any widening would let "
        "the pass run the full regeneration command instead of the pruner"
    )


def test_a_repairable_sentinel_carries_no_measurement_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The note is for artifacts a checkout cannot produce, not for all of them."""

    _install_fakes(
        monkeypatch,
        changed=["backend/testing/test_new_thing.py"],
        current=JUnitReport(failures={_QUEUE_ROUTE: "1181 ranked, 1182 collected"}),
        baseline=JUnitReport(),
        current_exit=1,
    )

    assert lane._run_lane(REPO_ROOT, "automain") == 1
    assert "note:" not in capsys.readouterr().err


def test_the_developer_gate_runs_this_lane_on_every_path_before_the_corpus() -> None:
    """``./check`` must enforce -- and repair -- freshness in BOTH modes.

    The developer host has no ``./pr open``: ``scripts/check-default.sh`` is the
    only in-repo publication gate it has, and before this lane its always-on
    portion covered exactly one derived artifact (the size cap ledger).

    Two structural properties, both load-bearing:

    * the lane is NOT nested under a ``WITH_TESTS`` branch, so the lint-only
      path enforces it and the ``./check test`` path repairs on it;
    * it runs BEFORE the static corpus. Under ``./check test`` the corpus runs
      the whole roster, so a repair applied after it would be graded against
      the pre-repair tree and fail the gate anyway.
    """

    gate = (REPO_ROOT / "scripts" / "check-default.sh").read_text(encoding="utf-8")

    assert "python3 scripts/sentinel_freshness.py --fix" in gate
    invocation = gate.index("python3 scripts/sentinel_freshness.py")
    opener = gate.rindex("source scripts/backend-test-route.sh", 0, invocation)
    assert 'WITH_TESTS' not in gate[opener:invocation], (
        "the freshness lane must run on both ./check and ./check test; a "
        "WITH_TESTS branch above it is how it stops covering the lint-only path"
    )
    assert invocation < gate.index("python3 scripts/static_release_guard.py")
    # And its absence must fail closed rather than silently skip the gate.
    assert 'FAILED+=("sentinel freshness helper missing")' in gate
