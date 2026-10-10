#!/usr/bin/env python3
"""Resolve merge conflicts that are confined to derived artifacts, without an agent.

THE DEFECT THIS CLOSES
----------------------
Two derived artifacts are committed to this repository and are rewritten by
essentially every PR that touches the test estate:

* ``backend/testing/test-estate-ranked-queue.md`` -- a ~1200-line generated report. FIXED AT
  THE SOURCE 2026-08-26: its ``Rank`` ordinal column and its estate-summary
  table were GLOBAL fields that rewrote the whole table when any test file was
  added or resized, so two concurrent PRs could not both touch the estate
  without colliding. Both are gone. Every row is now a function of exactly one
  test file, so two branches adding two files produce disjoint insertions and
  the ordinary three-way merge reproduces a fresh ``--write`` byte for byte --
  under stock git, and therefore on GitHub too. The entry below stays as the
  fallback for the residual case (two branches adding files that rank
  adjacently, or a hand-edit).
* ``backend/.test_durations`` -- a ~21000-line sorted JSON map. Both sides of a
  typical merge only INSERT keys, yet git still reports a textual conflict,
  because appending at the end of a JSON object rewrites the previous last
  line (it gains a comma).

The consequence is structural, not occasional: every merge into ``automain``
re-conflicts every other open PR on these two paths. On 2026-08-25 merging
#17939, #17952 and #17955 forced a manual re-sync of the others each time, and
every re-sync restarts CI (5-8 minutes a round).

The expensive half is what the pipeline then does with that conflict.
``orchestrator_ci_monitor.py`` sees the PR go unmergeable and spawns a full
``fix`` pipeline task (``review_loop.conflict_fix_spawned``): a provisioned VM
plus a Claude turn. ``agent/src/handlers/review-fix.ts`` rebases, aborts on
failure, re-merges specifically so that Claude can see conflict markers, and
hands the markers to the model. So a conflict whose correct resolution is
100% mechanical -- regenerate a pure report, union two insert-only maps --
burns an LLM turn and a VM boot, then force-pushes and restarts CI.

This module makes that class free. It resolves the conflict deterministically
and refuses, loudly and without touching the tree, to guess at anything else.

MEASURED FREQUENCY (2026-08-26)
-------------------------------
Twenty real ``origin/automain`` re-sync merges from automain's history were
replayed from their two parents with STOCK drivers -- which is exactly what a
developer clone has, and exactly how GitHub computes ``mergeable`` server-side.
Nine conflicted. ``backend/testing/test-estate-ranked-queue.md`` accounted for eight of those
nine (40% of all merges); ``module_size_limits.json`` for two and
``cap_bump_history.json`` for one; only three carried a real source conflict.
So the dominant cost of a merge in this repository was not merging code.

That 40% is precisely what the ranked queue's line-local rewrite removes. With
the ordinal and the summary gone, those eight replayed conflicts become disjoint
single-line insertions that merge with no driver, no resolver and no
regeneration step. What is left for this module is the genuinely unmergeable
residue: the measurement map and the cap file.

WHY THESE PATHS, AND ONLY THESE
-------------------------------
The governing policy is ``scripts/derived_artifact_repair.py``'s module
docstring, which sorts derived artifacts by whether they encode a DECISION.
This module answers a strictly narrower question than that one -- not "may
this artifact be regenerated unattended?" but "is there exactly one correct
merge of these two versions, derivable without judgement?" -- so it inherits
that policy's taxonomy rather than restating it:

* A **ratchet, floor or snapshot** encodes a decision, and a merge of two
  decisions is itself a decision. Never mechanical, and ``NEVER_MECHANICAL``
  pins the reason per artifact.

* A **cap** would be one of those, except that this repository has already
  decided otherwise IN CODE: ``backend/testing/merge_size_limits.py`` is the
  registered ``sizecap`` merge driver, it max-unions competing caps, and
  ``rollup_size_preflight.py`` raises caps unattended as well. So
  ``module_size_limits.json`` is registered here by REPLAYING that driver over
  the merge stages -- not by reimplementing it, and not by ruling on it a
  second time. Until this entry existed, the two modules contradicted each
  other in writing: ``.gitattributes`` said the conflict auto-resolves while
  ``NEVER_MECHANICAL`` said an agent must never resolve it unattended, and the
  only effect of that disagreement was that a conflict the driver would have
  prevented reached an LLM whenever it materialized somewhere the driver was
  not registered.

* A **pure report** encodes neither a decision nor an observation. It is a
  total function of the tree, so the conflicted *content* is irrelevant: the
  one correct answer is whatever the generator emits from the merged tree.
  ``backend/testing/test-estate-ranked-queue.md`` is this, and ``derived_artifact_repair``
  already licenses regenerating it unattended for the same reason.

* A **measurement** encodes an observation nothing in a checkout can
  reproduce, which is exactly why ``derived_artifact_repair`` refuses to
  REGENERATE ``backend/.test_durations``. Merging is a different operation
  from regenerating, and it is safe precisely where regeneration is not:
  taking the union of two measurement sets fabricates nothing, because every
  entry in the result was measured by one side or the other. That safety is
  conditional, so this module VERIFIES the condition instead of assuming it
  (see ``union_insert_only``) and declines the merge when it does not hold.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It never partially resolves. Every unmerged path is classified BEFORE
anything is written, and a single unregistered path aborts the whole pass with
the tree exactly as the merge left it -- because a half-staged tree is worse
than an unresolved one for the agent that inherits it.

It never invents a measurement. ``union_insert_only`` proves both sides are
supersets of the merge base before unioning, so a side that re-timed or
dropped an existing entry is handed to a human rather than silently discarded.

It never regenerates an artifact that is not conflicted. Staleness that no
merge caused belongs to ``scripts/sentinel_freshness.py``.

THREE ENTRY POINTS, ONE POLICY
------------------------------
``--merge-driver %O %A %B`` is the git custom merge driver for
``backend/.test_durations``, registered exactly like the existing ``sizecap``
driver for ``module_size_limits.json`` (``.gitattributes`` plus a
``merge.testdurations.driver`` git-config entry, because a clone does not carry
git config). It stops the conflict from ever materializing during a local
rebase or merge.

``--register-drivers`` installs BOTH drivers into whatever clone it is run in.
``.gitattributes`` is only half the wiring and the other half used to be
installed in exactly one place -- ``agent/src/index.ts``, at agent
workspace-prep time -- so the fleet had the drivers and a developer clone had
them nowhere. Every hand-resolution of these paths on a developer host was this
repository's own machinery being absent at the point of use. The developer gate
(``scripts/check-default.sh``) and ``scripts/resolve-derived-conflicts.sh`` now
call it, so a clone that has ever run either one has the drivers live.

``--resolve`` is the recovery pass for a conflict that materialized anyway --
because GitHub computes ``mergeable`` server-side with stock drivers and never
consults this one, so a PR still goes unmergeable and the orchestrator still
spawns a fix task. Measured 2026-08-26: the server honours no ``.gitattributes``
merge attribute at all, not even a BUILT-IN one. A two-commit probe whose only
change was each side appending one line to a path marked ``merge=union`` merges
cleanly under stock local git and is reported CONFLICTING by the GitHub API
(throwaway PR #18024). So a driver can never be the whole answer for a path that
must merge on the server -- which is why the ranked queue was fixed in its
RENDERING rather than given one.

``--resolve`` also still covers ``backend/testing/test-estate-ranked-queue.md``, which could
never have been a merge driver in any case: a driver is handed three blobs, and
the one correct resolution here is a function of the whole MERGED TREE, which
does not exist yet at driver time.

Both spellings share :func:`union_insert_only`, so the insert-only premise is
proven in exactly one place and cannot drift between them.

DISPROVEN ALTERNATIVE: ``merge=union``
--------------------------------------
The stock ``union`` driver is NOT a safe spelling of this, and the failure is
silent. Given a base of ``{"a":1,"b":2,"y":3}`` where each side appends one key,
git reports ``Merge made by the 'ort' strategy`` -- success -- and emits::

    "t/y.py::test_y": 3.0,
    "t/z_ours.py::test_zo": 9.0
    "t/y.py::test_y": 3.0,
    "t/z_theirs.py::test_zt": 8.0

which is invalid JSON (a missing comma) and carries a duplicate key. Appending
to a JSON object rewrites the previous last line to add a comma, so both sides
edit that line and union keeps both copies. A resolution that lands broken JSON
on the integration branch under a green merge is strictly worse than the
conflict it replaces, so this module parses and re-renders instead.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "MechanicalResolution",
    "MergeDriver",
    "NotInsertOnly",
    "ResolveOutcome",
    "MECHANICAL_RESOLVERS",
    "MERGE_DRIVERS",
    "NEVER_MECHANICAL",
    "classify_conflicts",
    "register_merge_drivers",
    "union_insert_only",
    "render_durations",
    "resolve_merge",
]


# Resolution phases, applied in ascending order. A regenerated report is a
# function of the merged tree, and the ranked queue reads backend/.test_durations
# for its measured-cost column, so every content merge must land before any
# generator runs or the report is derived from a half-merged tree.
PHASE_CONTENT_MERGE = 0
PHASE_REGENERATE = 1


@dataclass(frozen=True)
class MechanicalResolution:
    """A conflict whose correct resolution is derivable without judgement."""

    path: str
    """Repo-relative path this entry resolves."""

    strategy: str
    """``json_insert_union`` or ``regenerate``."""

    why: str
    """Why exactly one correct merge exists, i.e. why no judgement is needed."""

    phase: int = PHASE_CONTENT_MERGE
    """Ordering key; see the phase constants above."""

    regenerate: tuple[str, ...] = ()
    """For ``regenerate``: script + args, run with the current interpreter."""

    verify: tuple[str, ...] = ()
    """For ``regenerate``: a command that must exit 0 once the artifact is written."""

    driver: tuple[str, ...] = ()
    """For ``sizecap_union``: the repo-relative git merge driver to run as ``%O %A %B``.

    Named separately from ``regenerate`` because it is a different operation on
    different inputs: a generator derives the artifact from the merged TREE,
    whereas a driver is handed the three merge STAGES and never reads the tree.
    """


MECHANICAL_RESOLVERS: dict[str, MechanicalResolution] = {
    "backend/.test_durations": MechanicalResolution(
        path="backend/.test_durations",
        strategy="json_insert_union",
        phase=PHASE_CONTENT_MERGE,
        why=(
            "A flat measurement map keyed by pytest node id. Concurrent "
            "branches add entries for the tests they add; the union is "
            "therefore lossless and fabricates nothing, because every entry "
            "in the result was measured on one side or the other. The union "
            "is only correct while both sides are INSERT-ONLY, so that "
            "premise is proven against the merge base per merge rather than "
            "assumed -- a re-timed or deleted entry is a real divergence and "
            "is handed back unresolved."
        ),
    ),
    "backend/testing/test-estate-ranked-queue.md": MechanicalResolution(
        path="backend/testing/test-estate-ranked-queue.md",
        strategy="regenerate",
        phase=PHASE_REGENERATE,
        why=(
            "A pure report: render_queue() is a total function of the "
            "checked-out tree and no consumer reads it (not backend/app, not "
            "the agent, not CI sharding, not the runtime). The conflicted "
            "text on either side is therefore not evidence of anything -- the "
            "one correct answer is whatever the generator emits from the "
            "merged tree, which is also the entire correct human response. "
            "scripts/derived_artifact_repair.py registers it AUTOREPAIRABLE "
            "on the same reasoning."
        ),
        regenerate=("scripts/generate_test_estate_ranked_queue.py", "--write"),
        verify=("scripts/generate_test_estate_ranked_queue.py", "--check"),
    ),
    "backend/testing/module_size_limits.json": MechanicalResolution(
        path="backend/testing/module_size_limits.json",
        strategy="sizecap_union",
        phase=PHASE_CONTENT_MERGE,
        why=(
            "Not a new judgement: this repository ALREADY resolves this exact "
            "conflict mechanically. backend/testing/merge_size_limits.py is the "
            "registered `sizecap` merge driver (.gitattributes) and performs a "
            "max-cap union, and rollup_size_preflight.py appends auto-bumps of "
            "its own. Declining it here only meant that a conflict which the "
            "driver would have prevented -- but which materialized anyway, "
            "because GitHub computes mergeability server-side with STOCK "
            "drivers -- reached an LLM instead. This entry runs that same "
            "driver over the three merge stages, so there is one implementation "
            "and one policy rather than two that disagree."
        ),
        driver=("backend/testing/merge_size_limits.py",),
    ),
}


NEVER_MECHANICAL: dict[str, str] = {
    "backend/testing/conversational_workday_manifest.json": (
        "The per-file test_count entries are protection FLOORS. Merging two "
        "floors means choosing which protection level survives, which is the "
        "decision the floor exists to force."
    ),
    "backend/testing/event_type_snapshot.json": (
        "A pinned literal set -- a RATCHET. Unioning two sides silently "
        "ratifies every event_type both of them added, which is precisely the "
        "human ratification the snapshot exists to require."
    ),
    "backend/testing/cap_bump_history.json": (
        "An append-only ledger of cap bumps, keyed by commit sha, that the "
        "deployed backend reads INSTEAD of git log. A union of two sides is "
        "very likely correct and is worth building, but no shipped driver "
        "already performs it, so doing it here would be this module inventing "
        "a policy rather than reusing one. Repair it with "
        "`python3 backend/testing/check_size_limits.py --fix`, which rebuilds "
        "the ledger from git."
    ),
    "admin-ui/.test_durations.json": (
        "Unlike backend/.test_durations these are regenerated wholesale by a "
        "timed vitest run, so a conflicting side is usually a full re-time "
        "rather than an insert. The insert-only premise that licenses the "
        "backend union does not hold, and unioning two whole-file re-times "
        "interleaves measurements from two different hosts."
    ),
    "agent/.test_durations.json": (
        "Same shape and same reason as admin-ui/.test_durations.json."
    ),
}


@dataclass(frozen=True)
class MergeDriver:
    """One custom git merge driver, as ``.gitattributes`` names it."""

    name: str
    """The ``merge=<name>`` value in .gitattributes, and the git-config key."""

    description: str
    """Stored as ``merge.<name>.name``; git prints it in merge diagnostics."""

    script: str
    """Repo-relative script the driver runs, invoked as ``%O %A %B``."""

    args: tuple[str, ...] = ()
    """Arguments between the script and ``%O %A %B``."""

    def command(self, python: str) -> str:
        """The ``merge.<name>.driver`` command line, quoted for git's shell split."""
        parts = [f'"{python}"', f'"{self.script}"', *self.args, "%O", "%A", "%B"]
        return " ".join(parts)


MERGE_DRIVERS: tuple[MergeDriver, ...] = (
    MergeDriver(
        name="sizecap",
        description="max-cap union merge for module_size_limits.json",
        script="backend/testing/merge_size_limits.py",
    ),
    MergeDriver(
        name="testdurations",
        description="insert-only union merge for backend/.test_durations",
        script="scripts/derived_artifact_merge_resolve.py",
        args=("--merge-driver",),
    ),
)


def register_merge_drivers(
    repo: Path, python: str | None = None
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Install every :data:`MERGE_DRIVERS` entry in ``repo``'s LOCAL git config.

    ``.gitattributes`` maps the paths to ``merge=sizecap`` / ``merge=testdurations``,
    but git IGNORES that mapping unless the driver command is registered in the
    repository's own config -- and a clone does not carry git config. Until this
    function ran somewhere, those two paths conflicted textually on every merge
    even though the repo ships a correct mechanical merge for both.

    That gap was not uniform: ``agent/src/index.ts`` installs the drivers at
    every agent workspace-prep site, so the FLEET has had them all along, while
    a developer clone had them nowhere. Every hand-resolution of these paths on
    a developer host was the pipeline's own machinery being absent at the point
    of use, which is why this is callable from the developer gate too.

    ``python`` defaults to the interpreter running this process rather than the
    literal ``python3``: Git Bash on a Windows developer host has no ``python3``
    (the python.org installer publishes ``python.exe`` only), so a hard-coded
    ``python3`` registers a driver that always fails -- and a failing driver is
    indistinguishable from an unregistered one except that it is slower.

    Idempotent: ``git config`` overwrites the keys. Best-effort and PER DRIVER,
    matching ``registerDerivedArtifactMergeDrivers`` in the agent -- failing to
    register one only means that path falls back to the normal conflict route,
    which is strictly today's behaviour, so a partial success is worth keeping.

    Returns ``(registered_names, failures_by_name)``.
    """
    interpreter = python or sys.executable
    registered: list[str] = []
    failures: dict[str, str] = {}
    for driver in MERGE_DRIVERS:
        try:
            _git(repo, ["config", f"merge.{driver.name}.name", driver.description])
            _git(
                repo,
                ["config", f"merge.{driver.name}.driver", driver.command(interpreter)],
            )
        except RuntimeError as exc:
            failures[driver.name] = str(exc)
            continue
        registered.append(driver.name)
    return tuple(registered), failures


class NotInsertOnly(ValueError):
    """Raised when a side of a duration merge changed or dropped a base entry."""


@dataclass(frozen=True)
class ResolveOutcome:
    """What a resolve pass did, in enough detail to report and to fail closed."""

    resolved: tuple[str, ...] = ()
    """Conflicted paths this pass staged a mechanical resolution for."""

    unhandled: tuple[str, ...] = ()
    """Conflicted paths with no mechanical resolution; nothing was mutated."""

    details: dict[str, str] = field(default_factory=dict)
    """Path -> one-line description of what was done, for the PR/agent log."""

    error: str | None = None
    """Set when the pass could not complete; the caller must not treat it as done."""

    @property
    def complete(self) -> bool:
        """True only when every conflict was resolved mechanically."""
        return not self.unhandled and self.error is None and bool(self.resolved)

    def to_json(self) -> dict[str, object]:
        return {
            "resolved": list(self.resolved),
            "unhandled": list(self.unhandled),
            "details": dict(self.details),
            "error": self.error,
            "complete": self.complete,
        }


def classify_conflicts(
    paths: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split conflicted ``paths`` into (mechanically resolvable, everything else).

    Pure and total: it consults only :data:`MECHANICAL_RESOLVERS`, so the
    decision to hand a merge to an agent can be tested without a git tree.
    Paths are returned in resolution order -- content merges before generator
    runs -- and unhandled paths sorted, so callers can report them stably.
    """
    handled: list[str] = []
    unhandled: list[str] = []
    for path in paths:
        normalized = path.strip().replace("\\", "/")
        if not normalized:
            continue
        if normalized in MECHANICAL_RESOLVERS:
            handled.append(normalized)
        else:
            unhandled.append(normalized)
    handled.sort(key=lambda p: (MECHANICAL_RESOLVERS[p].phase, p))
    return tuple(handled), tuple(sorted(set(unhandled)))


def union_insert_only(
    base: Mapping[str, float],
    ours: Mapping[str, float],
    theirs: Mapping[str, float],
) -> dict[str, float]:
    """Union two insert-only measurement maps, or refuse.

    The union is lossless only while neither side changed or removed an entry
    that was present in the merge ``base``. That premise is what makes this a
    mechanical merge rather than a judgement call, so it is PROVEN here per
    merge instead of assumed: a side that re-timed or deleted a base entry
    raises :class:`NotInsertOnly` and the whole pass declines.

    ``base`` is empty for an add/add conflict (both branches created the file),
    which satisfies the premise vacuously and is the correct reading -- with no
    common ancestor, every entry on both sides is an insertion.

    When both sides insert the SAME key with different timings, both are
    honest measurements of the same node and neither is more correct; ``ours``
    wins deterministically so the merge is reproducible.
    """
    for label, side in (("ours", ours), ("theirs", theirs)):
        for key, value in base.items():
            if key not in side:
                raise NotInsertOnly(
                    f"{label} dropped {key!r}, which the merge base measured; "
                    "a deletion is a real divergence, not an insertion"
                )
            if side[key] != value:
                raise NotInsertOnly(
                    f"{label} re-timed {key!r} ({value!r} -> {side[key]!r}); "
                    "a changed measurement is a real divergence, not an insertion"
                )
    merged = dict(theirs)
    merged.update(ours)
    return merged


def render_durations(durations: Mapping[str, float]) -> str:
    """Render a duration map in the artifact's exact on-disk shape.

    ``indent=2``, ``sort_keys=True`` and one trailing newline, matching what
    the pytest-split writer and every existing revision of the file produce.
    Callers must write this with ``newline=""`` so the LF endings survive on
    Windows -- a CRLF rewrite would present as a whole-file diff and conflict
    against every open branch, which is the defect this module exists to end.
    """
    return json.dumps(durations, indent=2, sort_keys=True) + "\n"


def _git(repo: Path, args: Sequence[str]) -> str:
    # ``-c safe.directory=<repo>``: this module runs as a git MERGE DRIVER and
    # as a post-merge resolver, i.e. as a CHILD of another git process. A parent
    # that was itself made to work with ``git -c safe.directory=... merge`` does
    # NOT pass that down -- ``-c`` is per-invocation, not environment -- so the
    # merge succeeds and the driver's own reads die with "detected dubious
    # ownership" (exit 128). ``_git`` raises on any non-zero, so that surfaces
    # as "this conflict is not mechanically resolvable" and the caller escalates
    # to an LLM fix task for a conflict this module could have resolved. The
    # raise stays; the read is what gets repaired.
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    return result.stdout


def unmerged_paths(repo: Path) -> tuple[str, ...]:
    """Every path git currently reports as unmerged, normalized to forward slashes."""
    out = _git(repo, ["diff", "--name-only", "--diff-filter=U"])
    return tuple(
        line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()
    )


def _read_stage(repo: Path, stage: int, path: str) -> str | None:
    """Read one merge stage (1=base, 2=ours, 3=theirs), or None when absent."""
    # Same ownership hazard as ``_git`` -- and worse here, because a stage this
    # read cannot see is indistinguishable from a stage that does not exist:
    # ``None`` is read by the caller as an add/delete conflict ("a real
    # divergence, not an insertion") rather than as evidence loss.
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "show", f":{stage}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _resolve_json_insert_union(repo: Path, path: str) -> str:
    """Stage the union of both sides of a conflicted insert-only duration map."""
    raw_ours = _read_stage(repo, 2, path)
    raw_theirs = _read_stage(repo, 3, path)
    if raw_ours is None or raw_theirs is None:
        raise RuntimeError(
            f"{path}: expected both sides of the conflict to exist "
            f"(ours={'present' if raw_ours else 'absent'}, "
            f"theirs={'present' if raw_theirs else 'absent'}); "
            "an add/delete conflict is a real divergence, not an insertion"
        )
    raw_base = _read_stage(repo, 1, path)
    base = json.loads(raw_base) if raw_base else {}
    ours = json.loads(raw_ours)
    theirs = json.loads(raw_theirs)
    merged = union_insert_only(base, ours, theirs)
    target = repo / path
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(render_durations(merged))
    _git(repo, ["add", "--", path])
    added_ours = len(set(ours) - set(base))
    added_theirs = len(set(theirs) - set(base))
    return (
        f"union of insert-only measurement maps: base={len(base)} "
        f"+ours={added_ours} +theirs={added_theirs} -> {len(merged)} entries"
    )


def _resolve_sizecap_union(repo: Path, entry: MechanicalResolution) -> str:
    """Replay this path's registered git merge driver over the three merge stages.

    Deliberately a SUBPROCESS of the very script ``.gitattributes`` names, not a
    reimplementation of it and not an import of ``merge_limits``. The driver
    already ships, is already registered on every agent workspace, and already
    owns the max-cap policy and the byte-exact writer that keeps its output
    identical to ``check_size_limits.write_limits``. Running it verbatim is what
    makes this entry a REACH of an existing decision rather than a second one:
    there is no second implementation that can drift, and the driver's own
    fail-closed contract (any non-zero exit leaves the conflict) is inherited
    rather than restated.
    """
    script = repo / entry.driver[0]
    if not script.is_file():
        raise RuntimeError(f"merge driver not found: {entry.driver[0]}")
    raw_ours = _read_stage(repo, 2, entry.path)
    raw_theirs = _read_stage(repo, 3, entry.path)
    if raw_ours is None or raw_theirs is None:
        raise RuntimeError(
            f"{entry.path}: expected both sides of the conflict to exist "
            f"(ours={'present' if raw_ours else 'absent'}, "
            f"theirs={'present' if raw_theirs else 'absent'}); "
            "an add/delete conflict is a real divergence, not a competing cap"
        )
    raw_base = _read_stage(repo, 1, entry.path)
    with tempfile.TemporaryDirectory() as tmp:
        stage_paths = []
        for label, payload in (
            ("O", raw_base or ""),
            ("A", raw_ours),
            ("B", raw_theirs),
        ):
            stage = Path(tmp) / label
            # newline="" so the bytes handed to the driver are exactly the bytes
            # git staged. A CRLF rewrite here would make the resolution differ
            # from what the driver produces during a real merge on the same input.
            with open(stage, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
            stage_paths.append(str(stage))
        result = subprocess.run(
            [sys.executable, str(script), *entry.driver[1:], *stage_paths],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{entry.driver[0]} exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:400]}"
            )
        # %A is both OUR version and the path the driver writes its resolution to.
        resolved = Path(stage_paths[1]).read_text(encoding="utf-8")
    try:
        merged = json.loads(resolved)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{entry.driver[0]} exited 0 but emitted invalid JSON ({exc}); "
            "refusing to stage a resolution that would land broken"
        ) from exc
    with open(repo / entry.path, "w", encoding="utf-8", newline="") as handle:
        handle.write(resolved)
    _git(repo, ["add", "--", entry.path])
    sections = sum(1 for value in merged.values() if isinstance(value, dict))
    return (
        f"max-cap union via the registered {entry.driver[0]} merge driver "
        f"({sections} section(s))"
    )


def _resolve_regenerate(repo: Path, entry: MechanicalResolution) -> str:
    """Regenerate a pure report from the merged tree and stage the result."""
    script = repo / entry.regenerate[0]
    if not script.is_file():
        raise RuntimeError(f"generator not found: {entry.regenerate[0]}")
    result = subprocess.run(
        [sys.executable, str(script), *entry.regenerate[1:]],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(entry.regenerate)} exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    if not (repo / entry.path).is_file():
        raise RuntimeError(
            f"{' '.join(entry.regenerate)} exited 0 but left {entry.path} absent; "
            "refusing to report a resolution that produced no artifact"
        )
    if entry.verify:
        check = subprocess.run(
            [sys.executable, str(repo / entry.verify[0]), *entry.verify[1:]],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check.returncode != 0:
            raise RuntimeError(
                f"{' '.join(entry.verify)} still fails after regeneration: "
                f"{(check.stderr or check.stdout).strip()[:400]}"
            )
    _git(repo, ["add", "--", entry.path])
    return f"regenerated from the merged tree via {' '.join(entry.regenerate)}"


def resolve_merge(repo: Path) -> ResolveOutcome:
    """Resolve every conflict in ``repo``'s in-progress merge, or resolve none.

    Fail-closed by construction. Classification happens first and completely:
    if any unmerged path has no registered mechanical resolution, the function
    returns with ``unhandled`` populated having written nothing, leaving the
    tree exactly as the merge left it for the agent or human that inherits it.
    A partially resolved tree would be strictly worse than an unresolved one --
    it hides which conflicts were real.
    """
    try:
        conflicts = unmerged_paths(repo)
    except RuntimeError as exc:
        return ResolveOutcome(error=str(exc))
    if not conflicts:
        return ResolveOutcome()

    handled, unhandled = classify_conflicts(conflicts)
    if unhandled:
        return ResolveOutcome(unhandled=unhandled)

    resolved: list[str] = []
    details: dict[str, str] = {}
    for path in handled:
        entry = MECHANICAL_RESOLVERS[path]
        try:
            if entry.strategy == "json_insert_union":
                details[path] = _resolve_json_insert_union(repo, path)
            elif entry.strategy == "sizecap_union":
                details[path] = _resolve_sizecap_union(repo, entry)
            elif entry.strategy == "regenerate":
                details[path] = _resolve_regenerate(repo, entry)
            else:  # pragma: no cover - guarded by the registry itself
                raise RuntimeError(f"unknown strategy {entry.strategy!r}")
        except (NotInsertOnly, RuntimeError, ValueError) as exc:
            return ResolveOutcome(
                resolved=tuple(resolved),
                unhandled=(path,),
                details=details,
                error=f"{path}: {exc}",
            )
        resolved.append(path)
    return ResolveOutcome(resolved=tuple(resolved), details=details)


def merge_driver(ancestor_path: str, ours_path: str, theirs_path: str) -> int:
    """Git custom merge driver for ``backend/.test_durations``.

    Git invokes this as ``%O %A %B``: ``%O`` is the common-ancestor blob, ``%A``
    is OUR version and also the OUTPUT path the driver must overwrite, and
    ``%B`` is THEIR version. Exit 0 means the conflict is resolved; ANY non-zero
    exit leaves the conflict in place for the normal path, which is the
    fail-closed behaviour every refusal below relies on.

    An absent or empty ``%O`` is git's spelling of "no common ancestor" and is
    read as an empty base, which makes every entry on both sides an insertion.
    Anything that is not a clean insert-only union -- unparseable input, a
    re-timed entry, a dropped entry -- returns 1 rather than guessing, so a real
    divergence still reaches ``--resolve`` and ultimately a human.
    """
    try:
        raw_base = Path(ancestor_path).read_text(encoding="utf-8").strip()
        base = json.loads(raw_base) if raw_base else {}
        ours = json.loads(Path(ours_path).read_text(encoding="utf-8"))
        theirs = json.loads(Path(theirs_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"derived_artifact_merge_resolve: cannot parse inputs ({exc}); "
            "leaving conflict unresolved",
            file=sys.stderr,
        )
        return 1
    try:
        merged = union_insert_only(base, ours, theirs)
    except NotInsertOnly as exc:
        print(
            f"derived_artifact_merge_resolve: not an insert-only merge ({exc}); "
            "leaving conflict unresolved",
            file=sys.stderr,
        )
        return 1
    # %A is both our version and the path git reads the resolution back from.
    with open(ours_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(render_durations(merged))
    return 0


def _repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    # Discovery has no root to name yet, so it names the directory it is asking
    # FROM. That covers the way this script is actually invoked (from the
    # workspace root, which is the toplevel); a caller standing in a
    # subdirectory of a foreign-owned checkout still fails, and still fails
    # CLOSED into the SystemExit below, whose documented escape is ``--repo``.
    cwd = Path.cwd()
    out = subprocess.run(
        ["git", "-c", f"safe.directory={cwd}", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        raise SystemExit("not inside a git repository (pass --repo)")
    return Path(out.stdout.strip()).resolve()


def main(argv: Sequence[str] | None = None) -> int:
    """Resolve derived-artifact merge conflicts in an in-progress merge.

    Exit codes are the contract the agent call site keys off:
      0  every conflict was resolved mechanically and staged
      1  a conflict is not mechanically resolvable, or the pass failed;
         in either case nothing was left half-resolved
      2  there is no conflict to resolve
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", help="Repository root (default: git rev-parse --show-toplevel)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the outcome as JSON on stdout"
    )
    parser.add_argument(
        "--merge-driver",
        nargs=3,
        metavar=("ANCESTOR", "OURS", "THEIRS"),
        help="Run as the git merge driver for backend/.test_durations (%%O %%A %%B)",
    )
    parser.add_argument(
        "--register-drivers",
        action="store_true",
        help=(
            "Install the repo's custom merge drivers in this clone's git config "
            "and exit. A clone does not carry git config, so .gitattributes "
            "alone leaves them inert."
        ),
    )
    args = parser.parse_args(argv)
    if args.merge_driver:
        return merge_driver(*args.merge_driver)
    repo = _repo_root(args.repo)

    if args.register_drivers:
        registered, failures = register_merge_drivers(repo)
        if args.json:
            print(
                json.dumps(
                    {"registered": list(registered), "failures": failures},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for name in registered:
                print(f"registered merge driver {name}")
            for name, reason in sorted(failures.items()):
                print(f"could not register merge driver {name}: {reason}", file=sys.stderr)
        # Partial registration is strictly better than none and matches the
        # agent's per-driver best-effort loop, so only a total failure is an
        # error the caller should act on.
        return 1 if failures and not registered else 0

    outcome = resolve_merge(repo)
    if args.json:
        print(json.dumps(outcome.to_json(), indent=2, sort_keys=True))
    else:
        for path in outcome.resolved:
            print(f"resolved {path}: {outcome.details.get(path, '')}")
        for path in outcome.unhandled:
            reason = NEVER_MECHANICAL.get(path)
            note = f" ({reason})" if reason else ""
            print(f"UNHANDLED CONFLICT: {path}{note}", file=sys.stderr)
        if outcome.error:
            print(f"error: {outcome.error}", file=sys.stderr)

    if outcome.error or outcome.unhandled:
        return 1
    if not outcome.resolved:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
