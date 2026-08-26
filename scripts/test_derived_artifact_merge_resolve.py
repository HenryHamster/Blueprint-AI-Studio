"""Tests for the mechanical derived-artifact merge resolver.

WHAT IS BEING PINNED, AND WHY IT NEEDS PINNING
----------------------------------------------
Every merge into ``automain`` re-conflicts every other open PR on two derived
paths, and the pipeline answers that conflict by spawning a ``fix`` task -- a
VM plus a Claude turn -- to resolve something with exactly one correct answer.
``derived_artifact_merge_resolve`` removes the agent from that loop, which
means its refusals now carry the weight the agent used to carry. Three
properties therefore have to hold, and each has a real failure mode behind it:

* **The union must be lossless and must fabricate nothing.** Unioning two
  measurement maps is safe only while both sides INSERT. A side that re-timed
  or dropped an entry is a real divergence, and a resolver that silently
  discarded it would fabricate a measurement -- the precise thing
  ``derived_artifact_repair`` refuses to do by never regenerating this file.
  ``union_insert_only`` proves the premise per merge; these tests prove it
  refuses when the premise is false, not merely that it unions when it is true.

* **It must never resolve PARTIALLY.** A tree with the mechanical conflicts
  staged and the real one left unmerged is strictly worse than an untouched
  one: it hides which conflicts were real from whoever inherits the tree. The
  fixture tests assert the durations file is still UNMERGED after a pass that
  saw one unregistered path, not merely that the pass reported failure.

* **The registry must stay closed.** A new derived artifact must be classified
  deliberately, not join the mechanical path by omission. The ratchet at the
  bottom pins the registered paths, their generators and their agreement with
  ``derived_artifact_repair``'s allowlist against the real repository.

The fixture repositories ship their OWN tiny generator at the registered
script path rather than mocking the subprocess, so the regenerate strategy is
exercised through the real code path -- argv, exit codes, the post-write
``--check`` verification and the ``git add``. The real generator is not run
here: it takes ~15s against this repo, and the scripts lane has a per-file time
budget that a 15s call has already blown once (the harness-budget-inversion
class). The ratchet still pins that the real generator exists and is the one
``derived_artifact_repair`` names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from derived_artifact_merge_resolve import (  # noqa: E402
    MECHANICAL_RESOLVERS,
    PHASE_CONTENT_MERGE,
    PHASE_REGENERATE,
    NEVER_MECHANICAL,
    NotInsertOnly,
    classify_conflicts,
    main,
    merge_driver,
    render_durations,
    resolve_merge,
    union_insert_only,
    unmerged_paths,
)
import derived_artifact_merge_resolve as dar  # noqa: E402
from derived_artifact_repair import AUTOREPAIRABLE  # noqa: E402

DURATIONS = "backend/.test_durations"
RANKED_QUEUE = "test-estate-ranked-queue.md"

# A stand-in for scripts/generate_test_estate_ranked_queue.py: a total function
# of the tree (it counts the test files it can see), with the same --write /
# --check contract. Small enough to keep the lane fast, real enough that the
# resolver's argv, exit-code and verification handling are genuinely exercised.
FIXTURE_GENERATOR = '''\
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-estate-ranked-queue.md"


def render():
    files = sorted(p.name for p in (ROOT / "backend" / "testing").glob("test_*.py"))
    return "# queue\\n" + "".join(f"- {name}\\n" for name in files)


if "--write" in sys.argv:
    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        fh.write(render())
elif "--check" in sys.argv:
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if current != render():
        print("ranked queue is stale", file=sys.stderr)
        sys.exit(1)
else:
    print("need --write or --check", file=sys.stderr)
    sys.exit(2)
'''


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 and args[0] not in {"merge"}:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
    return result.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _durations(**entries: float) -> str:
    return render_durations(entries)


@pytest.fixture()
def conflicted_repo(tmp_path: Path) -> Path:
    """A repo mid-merge, conflicting on both registered derived artifacts.

    Both branches add one backend test file and one measured duration for it --
    the exact concurrent-PR shape that re-conflicts every open PR on automain.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")

    _write(repo, "scripts/generate_test_estate_ranked_queue.py", FIXTURE_GENERATOR)
    _write(repo, "backend/testing/test_base.py", "def test_base():\n    pass\n")
    _write(repo, DURATIONS, _durations(**{"testing/test_base.py::test_base": 1.5}))
    _write(repo, RANKED_QUEUE, "# queue\n- test_base.py\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    def branch(name: str, test_file: str, node: str, seconds: float) -> None:
        _git(repo, "checkout", "-q", "-b", name, "main")
        _write(repo, f"backend/testing/{test_file}", "def test_x():\n    pass\n")
        _write(
            repo,
            DURATIONS,
            _durations(
                **{"testing/test_base.py::test_base": 1.5, node: seconds}
            ),
        )
        _write(repo, RANKED_QUEUE, f"# queue\n- test_base.py\n- {test_file}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", name)

    branch("ours", "test_ours.py", "testing/test_ours.py::test_x", 2.5)
    branch("theirs", "test_theirs.py", "testing/test_theirs.py::test_x", 3.5)

    _git(repo, "checkout", "-q", "ours")
    _git(repo, "merge", "theirs", "--no-edit")
    return repo


# --------------------------------------------------------------------------
# union_insert_only: the premise that licenses a mechanical measurement merge
# --------------------------------------------------------------------------


def test_union_keeps_every_entry_from_both_sides():
    base = {"a": 1.0}
    merged = union_insert_only(base, {"a": 1.0, "b": 2.0}, {"a": 1.0, "c": 3.0})
    assert merged == {"a": 1.0, "b": 2.0, "c": 3.0}


def test_union_with_no_merge_base_is_an_add_add_and_unions():
    """Both branches created the file: with no ancestor, everything is an insert."""
    assert union_insert_only({}, {"b": 2.0}, {"c": 3.0}) == {"b": 2.0, "c": 3.0}


def test_union_refuses_when_a_side_retimed_a_base_entry():
    """A re-measurement is a real divergence; unioning would discard one timing."""
    with pytest.raises(NotInsertOnly, match="re-timed"):
        union_insert_only({"a": 1.0}, {"a": 9.9, "b": 2.0}, {"a": 1.0, "c": 3.0})


def test_union_refuses_when_the_other_side_retimed_a_base_entry():
    with pytest.raises(NotInsertOnly, match="re-timed"):
        union_insert_only({"a": 1.0}, {"a": 1.0, "b": 2.0}, {"a": 9.9, "c": 3.0})


def test_union_refuses_when_a_side_dropped_a_base_entry():
    """Rot-pruning is a deliberate edit, not an insertion; do not silently undo it."""
    with pytest.raises(NotInsertOnly, match="dropped"):
        union_insert_only({"a": 1.0, "d": 4.0}, {"a": 1.0, "b": 2.0}, {"a": 1.0, "d": 4.0, "c": 3.0})


def test_union_prefers_ours_when_both_sides_measured_the_same_node():
    """Both timings are honest; the tie-break only has to be deterministic."""
    assert union_insert_only({}, {"n": 2.0}, {"n": 3.0}) == {"n": 2.0}


def test_render_durations_matches_the_artifact_shape_with_lf_only():
    text = render_durations({"b": 2.0, "a": 1.0})
    assert text == '{\n  "a": 1.0,\n  "b": 2.0\n}\n'
    assert "\r" not in text, "a CRLF rewrite would conflict against every open branch"


# --------------------------------------------------------------------------
# classify_conflicts: which merges are mechanical at all
# --------------------------------------------------------------------------


def test_classify_splits_registered_paths_from_everything_else():
    handled, unhandled = classify_conflicts(
        [DURATIONS, "backend/app/models.py", RANKED_QUEUE]
    )
    assert set(handled) == {DURATIONS, RANKED_QUEUE}
    assert unhandled == ("backend/app/models.py",)


def test_classify_orders_content_merges_before_generator_runs():
    """The ranked queue reads the durations file, so it must regenerate last.

    generate_test_estate_ranked_queue.py opens ``backend/.test_durations``
    (BACKEND_DURATIONS_PATH) to rank the estate, so regenerating before that
    file is merged would derive the queue from a half-merged map.
    """
    handled, _ = classify_conflicts([RANKED_QUEUE, DURATIONS])
    assert handled == (DURATIONS, RANKED_QUEUE)


def test_classify_orders_by_phase_and_not_by_path_name(monkeypatch):
    """Pin the ORDERING RULE, not the accident of these two path names.

    ``backend/.test_durations`` happens to sort alphabetically before
    ``test-estate-ranked-queue.md``, so a sort key that dropped ``phase``
    entirely still produces the right order for today's registry -- the
    assertion above passes against that broken code. This registers a
    content-merge path that sorts LAST by name, so only a phase-aware sort can
    put it first, and a third path pins that ties still break by name.
    """
    registry = dict(MECHANICAL_RESOLVERS)
    template = registry[DURATIONS]
    registry["zzz-merged-last-by-name"] = replace(template, phase=PHASE_CONTENT_MERGE)
    registry["aaa-generated-first-by-name"] = replace(
        registry[RANKED_QUEUE], phase=PHASE_REGENERATE
    )
    monkeypatch.setattr(
        "derived_artifact_merge_resolve.MECHANICAL_RESOLVERS", registry
    )

    handled, _ = classify_conflicts(
        ["aaa-generated-first-by-name", "zzz-merged-last-by-name", DURATIONS]
    )

    assert handled == (
        DURATIONS,                      # phase 0, first by name
        "zzz-merged-last-by-name",      # phase 0, last by name -> still before phase 1
        "aaa-generated-first-by-name",  # phase 1, despite sorting first by name
    )


def test_classify_normalizes_windows_separators():
    handled, unhandled = classify_conflicts(["backend\\.test_durations"])
    assert handled == (DURATIONS,) and unhandled == ()


def test_classify_treats_decision_bearing_artifacts_as_unhandled():
    """A ratchet or floor must reach a human even though it is 'derived'."""
    _, unhandled = classify_conflicts(list(NEVER_MECHANICAL))
    assert set(unhandled) == set(NEVER_MECHANICAL)


# --------------------------------------------------------------------------
# resolve_merge: end to end over a real conflicted git tree
# --------------------------------------------------------------------------


def test_resolves_both_derived_conflicts_and_leaves_the_tree_committable(conflicted_repo):
    assert set(unmerged_paths(conflicted_repo)) == {DURATIONS, RANKED_QUEUE}

    outcome = resolve_merge(conflicted_repo)

    assert outcome.complete, outcome
    assert set(outcome.resolved) == {DURATIONS, RANKED_QUEUE}
    assert unmerged_paths(conflicted_repo) == ()

    merged = json.loads((conflicted_repo / DURATIONS).read_text(encoding="utf-8"))
    assert merged == {
        "testing/test_base.py::test_base": 1.5,
        "testing/test_ours.py::test_x": 2.5,
        "testing/test_theirs.py::test_x": 3.5,
    }
    raw = (conflicted_repo / DURATIONS).read_bytes()
    assert b"<<<<<<<" not in raw and b"\r\n" not in raw

    # Regenerated from the MERGED tree, so it names both sides' new test files.
    queue = (conflicted_repo / RANKED_QUEUE).read_text(encoding="utf-8")
    assert "test_ours.py" in queue and "test_theirs.py" in queue

    _git(conflicted_repo, "commit", "-q", "--no-edit")
    assert _git(conflicted_repo, "status", "--porcelain").strip() == ""


# --------------------------------------------------------------------------
# The checkout this runs over is routinely owned by someone else
# --------------------------------------------------------------------------
#
# This module runs as a git MERGE DRIVER, i.e. as a CHILD of another git
# process, and as a resolver invoked from the agent. ``-c safe.directory=`` is
# PER-INVOCATION: a parent `git -c safe.directory=... merge` does not pass it
# down, so the merge itself succeeds and the driver's own reads die with
# "detected dubious ownership" (exit 128). The failure is expensive rather than
# loud -- it reads as "this conflict is not mechanically resolvable" and
# escalates to a VM plus an LLM turn for a conflict with one correct answer.

_DUBIOUS_OWNERSHIP_RC = 128


def _foreign_owned_git_or_skip(root: Path) -> None:
    """Stage "another user owns this checkout" without needing a second uid.

    ``GIT_TEST_ASSUME_DIFFERENT_OWNER=1`` is git's own switch for exactly this
    condition, so a single-uid container reproduces the real CI failure. Skip
    rather than assert when this git cannot stage it.
    """
    env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    kw = {"cwd": root, "capture_output": True, "text": True, "env": env,
          "timeout": 15}
    bare = subprocess.run(["git", "rev-parse", "--show-toplevel"], **kw)
    safe = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "rev-parse", "--show-toplevel"], **kw
    )
    if bare.returncode != _DUBIOUS_OWNERSHIP_RC or safe.returncode != 0:
        pytest.skip("this git cannot stage the dubious-ownership condition here")
    assert "dubious ownership" in bare.stderr, bare.stderr


def test_resolves_a_merge_in_a_checkout_git_calls_foreign_owned(
    conflicted_repo, monkeypatch
):
    """Real conflicted tree, real exit-128 git, no mock of the git layer.

    Kills a dropped ``safe.directory`` on either read independently: without it
    on ``_git`` the pass raises out of ``unmerged_paths``; without it on
    ``_read_stage`` both sides read as absent and the union refuses as an
    add/delete divergence.
    """
    _foreign_owned_git_or_skip(conflicted_repo)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    outcome = resolve_merge(conflicted_repo)

    assert outcome.complete, outcome
    assert set(outcome.resolved) == {DURATIONS, RANKED_QUEUE}
    assert unmerged_paths(conflicted_repo) == ()
    merged = json.loads((conflicted_repo / DURATIONS).read_text(encoding="utf-8"))
    assert merged == {
        "testing/test_base.py::test_base": 1.5,
        "testing/test_ours.py::test_x": 2.5,
        "testing/test_theirs.py::test_x": 3.5,
    }


def test_repo_discovery_works_in_a_checkout_git_calls_foreign_owned(
    tmp_path, monkeypatch
):
    """``--repo`` is optional, so discovery must survive the same condition."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _foreign_owned_git_or_skip(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    assert dar._repo_root(None).resolve() == repo.resolve()


def test_repo_discovery_still_refuses_outside_a_repository(tmp_path, monkeypatch):
    """Hardening the READ must not soften the fail-closed CONSEQUENCE."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    with pytest.raises(SystemExit):
        dar._repo_root(None)


def test_refuses_and_mutates_nothing_when_any_conflict_is_not_mechanical(conflicted_repo):
    """The whole point of the resolver: one real conflict aborts the whole pass."""
    _git(conflicted_repo, "merge", "--abort")
    _git(conflicted_repo, "checkout", "-q", "ours")
    _write(conflicted_repo, "backend/app/models.py", "OURS\n")
    _git(conflicted_repo, "add", "-A")
    _git(conflicted_repo, "commit", "-qm", "ours code")
    _git(conflicted_repo, "checkout", "-q", "theirs")
    _write(conflicted_repo, "backend/app/models.py", "THEIRS\n")
    _git(conflicted_repo, "add", "-A")
    _git(conflicted_repo, "commit", "-qm", "theirs code")
    _git(conflicted_repo, "checkout", "-q", "ours")
    _git(conflicted_repo, "merge", "theirs", "--no-edit")

    before = (conflicted_repo / DURATIONS).read_bytes()
    outcome = resolve_merge(conflicted_repo)

    assert not outcome.complete
    assert outcome.resolved == ()
    assert "backend/app/models.py" in outcome.unhandled
    # Nothing staged, nothing rewritten: the agent inherits the tree untouched.
    assert set(unmerged_paths(conflicted_repo)) == {
        DURATIONS,
        RANKED_QUEUE,
        "backend/app/models.py",
    }
    assert (conflicted_repo / DURATIONS).read_bytes() == before


def test_refuses_when_a_side_retimed_an_existing_measurement(tmp_path):
    """A real divergence inside a registered path still reaches a human."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    _write(repo, DURATIONS, _durations(**{"testing/test_base.py::test_base": 1.5}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "-b", "theirs", "main")
    _write(repo, DURATIONS, _durations(**{"testing/test_base.py::test_base": 1.5, "testing/test_t.py::t": 3.0}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "theirs")

    _git(repo, "checkout", "-q", "main")
    # Ours re-times the shared entry instead of only inserting.
    _write(repo, DURATIONS, _durations(**{"testing/test_base.py::test_base": 42.0, "testing/test_o.py::o": 2.0}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ours")
    _git(repo, "merge", "theirs", "--no-edit")

    outcome = resolve_merge(repo)

    assert not outcome.complete
    assert outcome.unhandled == (DURATIONS,)
    assert "re-timed" in (outcome.error or "")
    assert unmerged_paths(repo) == (DURATIONS,)


def test_reports_nothing_to_do_on_a_clean_tree(conflicted_repo):
    _git(conflicted_repo, "merge", "--abort")
    outcome = resolve_merge(conflicted_repo)
    assert outcome.resolved == () and outcome.unhandled == ()
    assert not outcome.complete, "no conflict is not the same as a completed resolution"


def test_cli_exit_codes_are_the_contract_the_agent_keys_off(conflicted_repo, capsys):
    assert main(["--repo", str(conflicted_repo), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is True
    assert sorted(report["resolved"]) == sorted([DURATIONS, RANKED_QUEUE])

    # Second pass: the merge is resolved, so there is nothing left to do.
    assert main(["--repo", str(conflicted_repo), "--json"]) == 2


def test_cli_exits_nonzero_and_names_the_unhandled_path(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    _write(repo, "backend/app/models.py", "base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "theirs", "main")
    _write(repo, "backend/app/models.py", "theirs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "theirs")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "backend/app/models.py", "ours\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ours")
    _git(repo, "merge", "theirs", "--no-edit")

    assert main(["--repo", str(repo)]) == 1
    assert "UNHANDLED CONFLICT: backend/app/models.py" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The registry ratchet: no artifact joins the mechanical path by omission
# --------------------------------------------------------------------------


def test_registry_is_exactly_the_two_known_conflict_generators():
    """A literal pin. Adding a third mechanical merge must be a reviewed edit."""
    assert set(MECHANICAL_RESOLVERS) == {DURATIONS, RANKED_QUEUE}


def test_mechanical_and_never_mechanical_never_overlap():
    assert set(MECHANICAL_RESOLVERS).isdisjoint(NEVER_MECHANICAL)


def test_every_registered_entry_states_why_it_needs_no_judgement():
    for path, entry in MECHANICAL_RESOLVERS.items():
        assert entry.path == path
        assert entry.strategy in {"json_insert_union", "regenerate"}
        assert len(entry.why.strip()) > 80, f"{path} needs a real justification"
    for path, reason in NEVER_MECHANICAL.items():
        assert len(reason.strip()) > 40, f"{path} needs a real justification"


def test_registered_artifacts_and_generators_exist_in_this_repository():
    """Pins the real paths, so a rename cannot silently disable the resolver."""
    for path, entry in MECHANICAL_RESOLVERS.items():
        assert (REPO_ROOT / path).is_file(), f"{path} is registered but absent"
        for command in (entry.regenerate, entry.verify):
            if command:
                assert (REPO_ROOT / command[0]).is_file(), f"{command[0]} is missing"


def test_regenerate_strategy_agrees_with_the_repair_allowlist():
    """The two policy tables must not drift apart.

    ``derived_artifact_repair`` decides whether an artifact may be regenerated
    unattended; this module reuses that verdict for its regenerate strategy. If
    a future change demotes the ranked queue there, regenerating it here would
    be exactly the unratified rewrite that module refuses.
    """
    repair_generators = {
        artifact: (entry.script, *entry.args)
        for entry in AUTOREPAIRABLE.values()
        for artifact in entry.artifacts
    }
    for path, entry in MECHANICAL_RESOLVERS.items():
        if entry.strategy != "regenerate":
            continue
        assert path in repair_generators, (
            f"{path} is regenerated on the merge path but is no longer "
            "AUTOREPAIRABLE in derived_artifact_repair"
        )
        assert entry.regenerate == repair_generators[path]


def test_measurement_union_is_not_claimed_to_be_regenerable():
    """Merging a measurement is safe; regenerating one fabricates data."""
    assert MECHANICAL_RESOLVERS[DURATIONS].strategy == "json_insert_union"
    assert MECHANICAL_RESOLVERS[DURATIONS].regenerate == ()
    repair_artifacts = {
        artifact for entry in AUTOREPAIRABLE.values() for artifact in entry.artifacts
    }
    assert DURATIONS not in repair_artifacts


# --------------------------------------------------------------------------
# The git merge driver: the conflict never materializes in the first place
# --------------------------------------------------------------------------


def _driver_files(tmp_path: Path, base, ours, theirs) -> tuple[str, str, str]:
    """Write the three blobs git hands a merge driver as %O %A %B."""
    paths = []
    for name, payload in (("O", base), ("A", ours), ("B", theirs)):
        target = tmp_path / name
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write("" if payload is None else render_durations(payload))
        paths.append(str(target))
    return paths[0], paths[1], paths[2]


def test_merge_driver_writes_the_union_into_the_ours_path(tmp_path):
    ancestor, ours, theirs = _driver_files(
        tmp_path, {"a::x": 1.0}, {"a::x": 1.0, "b::y": 2.0}, {"a::x": 1.0, "c::z": 3.0}
    )
    assert merge_driver(ancestor, ours, theirs) == 0
    # %A is both our side and the path git reads the resolution back from.
    assert json.loads(Path(ours).read_text(encoding="utf-8")) == {
        "a::x": 1.0,
        "b::y": 2.0,
        "c::z": 3.0,
    }
    # Read bytes, not text. read_text() applies universal-newline translation,
    # which rewrites a stray CRLF to LF before the assertion can see it and
    # makes this check vacuous. The newline="" kwarg that would defeat that
    # translation only exists on Path.read_text() in Python 3.13+, and the
    # scripts test lane runs the backend image's Python 3.11 -- passing it
    # there raises TypeError. Bytes need no kwarg and hold on every version.
    assert b"\r" not in Path(ours).read_bytes()


def test_merge_driver_treats_an_empty_ancestor_as_no_common_ancestor(tmp_path):
    ancestor, ours, theirs = _driver_files(tmp_path, None, {"b::y": 2.0}, {"c::z": 3.0})
    assert merge_driver(ancestor, ours, theirs) == 0
    assert json.loads(Path(ours).read_text(encoding="utf-8")) == {"b::y": 2.0, "c::z": 3.0}


def test_merge_driver_leaves_the_conflict_when_a_side_retimed_an_entry(tmp_path):
    """Fail closed: a real divergence must reach the normal conflict path."""
    ancestor, ours, theirs = _driver_files(
        tmp_path, {"a::x": 1.0}, {"a::x": 99.0}, {"a::x": 1.0, "c::z": 3.0}
    )
    before = Path(ours).read_bytes()
    assert merge_driver(ancestor, ours, theirs) == 1
    assert Path(ours).read_bytes() == before, "a declined merge must not rewrite %A"


def test_merge_driver_leaves_the_conflict_on_unparseable_input(tmp_path):
    ancestor, ours, theirs = _driver_files(tmp_path, {}, {"b::y": 2.0}, {})
    Path(theirs).write_text("{ not json", encoding="utf-8")
    assert merge_driver(ancestor, ours, theirs) == 1


def test_merge_driver_registered_via_gitattributes_merges_with_no_conflict(tmp_path):
    """End-to-end proof of the wiring, and the counter-example to merge=union.

    The stock ``union`` driver reports SUCCESS on this exact input while
    emitting a duplicate key and a missing comma -- invalid JSON landed on the
    integration branch under a green merge. This asserts the registered driver
    produces a real merge instead: no conflict, valid JSON, both sides' entries.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    # Exactly the two halves .gitattributes + workspace prep install in the fleet.
    _write(repo, ".gitattributes", "backend/.test_durations merge=testdurations\n")
    _git(repo, "config", "merge.testdurations.name", "insert-only union merge")
    _git(
        repo,
        "config",
        "merge.testdurations.driver",
        f'"{sys.executable}" '
        f'"{REPO_ROOT / "scripts" / "derived_artifact_merge_resolve.py"}" '
        "--merge-driver %O %A %B",
    )

    _write(repo, DURATIONS, _durations(**{"testing/test_base.py::test_base": 1.5}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "-b", "theirs", "main")
    _write(repo, DURATIONS, _durations(**{
        "testing/test_base.py::test_base": 1.5,
        "testing/test_theirs.py::test_x": 3.5,
    }))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "theirs")

    _git(repo, "checkout", "-q", "main")
    _write(repo, DURATIONS, _durations(**{
        "testing/test_base.py::test_base": 1.5,
        "testing/test_ours.py::test_x": 2.5,
    }))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ours")

    _git(repo, "merge", "theirs", "--no-edit")

    assert unmerged_paths(repo) == (), "the driver should have resolved it outright"
    merged = json.loads((repo / DURATIONS).read_text(encoding="utf-8"))
    assert merged == {
        "testing/test_base.py::test_base": 1.5,
        "testing/test_ours.py::test_x": 2.5,
        "testing/test_theirs.py::test_x": 3.5,
    }


def test_gitattributes_registers_the_driver_for_the_durations_path():
    """Half the wiring lives in .gitattributes; pin it so a rename cannot drop it."""
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "backend/.test_durations merge=testdurations" in attributes
    assert "backend/.test_durations text eol=lf" in attributes
