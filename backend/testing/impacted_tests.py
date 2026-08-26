#!/usr/bin/env python3
"""Reverse-dependency resolver: changed repo paths -> impacted backend test files.

Fast, dependency-free (stdlib only), no DB / Docker / network — sub-second. It is
invoked BY REFERENCE from the merge-time gate the agent generates in
``agent/src/prompts/check-helper.ts`` (see CLAUDE.md's "Generating Code for One
Language From Another"): the generated ``./check`` pipes the changed-file list
into this module and runs the emitted backend test files under the sanctioned
Postgres harness. It is a plain module (no ``test_`` prefix) so pytest never
collects it.

Why it exists: the merge-time backend gate otherwise runs only the ~10-file
``--standalone --no-postgres`` smoke set. Test files that exercise the CHANGED
modules — by importing ``app.<module>`` or by mock-patching ``app.<module>...`` —
and even test files the PR itself ADDS, never run mechanically before merge, so a
contract break merges green on the task PR and only surfaces at the promotion-PR
full matrix, burning a long diagnosis+re-provision round-trip.

Contract
--------
* Changed repo-relative paths are read from STDIN, one per line (blank lines
  ignored). Positional CLI args, when given, are treated as additional changed
  paths — handy for direct unit tests. ``--repo-root`` overrides the inferred
  root (the repo root is two levels up from this file).
* The impacted set is:
    - every directly changed/added ``backend/testing/test_*.py`` that exists on
      disk (this is what makes a PR's OWN new tests run), PLUS
    - the existing conventional ``backend/testing/test_<module>.py`` for each
      directly changed top-level ``backend/app/<module>.py`` module. This keeps
      a conflict-resolution edit's same-module test within the bounded,
      attributed selector set even when that test uses no import that the
      reverse-dependency scan can observe, PLUS
    - for each changed ``backend/app/<...>.py``, every ``backend/testing/
      test_*.py`` that references that module. Both import forms
      (``import app.foo``, ``from app.foo import x``, ``from app import foo``,
      ``from app.routers import agent``) and string mock-patch targets
      (``patch("app.foo.bar")``, ``mocker.patch('app.foo...')``,
      ``patch.object(app.foo, ...)``) are detected. Matching respects module
      boundaries: ``app.foo`` must NOT match a longer sibling ``app.foobar``.
* Emitted deduped to STDOUT, one repo-relative path per line, ranked by
  relevance to the diff: directly changed/added test files first, then test
  files ordered by how close their referenced modules sit to the change (an
  import-hop distance from a changed module, with within-tier tiebreakers for
  mock-patch coupling, direct changed-module references, and closure breadth),
  and surface-glob drift sentinels last. This is what lets the merge gate's
  ``--limit``-style cap keep the tests most likely to catch a contract break.
  Empty set -> no output, exit 0. Optional ``--limit N`` truncates to the first
  N (most relevant); the overflow is reported to STDERR as ``deferred-rank:``
  lines (never silently dropped).
* ``--mandatory-output PATH`` writes the matched surface sentinels to a
  separate newline-delimited file while ``--limit`` applies only to ordinary
  selectors. This is the merge-gate protocol: mandatory sentinels are carried
  past the ordinary cap, and the output file is written fail-closed.
* ``--budget-seconds N`` additionally caps the ordinary set by SUMMED MEASURED
  COST, read from ``backend/.test_durations`` (override with ``--durations``).
  A count cap alone cannot bound runtime: the eight heaviest files in the
  committed estate sum to ~42 minutes, so an eight-selector gate can exceed any
  timeout it is given and be cascade-killed while perfectly healthy. Selection
  walks in relevance order, SKIPPING (not stopping at) an entry that does not
  fit, so one expensive high-rank file cannot starve the cheaper files behind
  it. Mandatory sentinels are charged against the budget but never dropped. The
  most relevant entry is kept even when it alone exceeds the budget — selecting
  nothing is the one outcome a merge gate must never produce — so the true
  bound is ``budget + cost of the single heaviest selected file``. Budgeting is
  fail-open: with no usable measurements it does not apply and says so on
  STDERR, because "requested and skipped" and "applied and dropped nothing"
  are otherwise indistinguishable on STDOUT. Its overflow is reported as
  ``deferred-budget:`` — a DIFFERENT token from the rank cap's on purpose. The
  two classes read alike and matter very differently: a ``deferred-rank:``
  entry is the long tail the protocol deliberately does not run (hundreds of
  files for a core module), while a ``deferred-budget:`` entry was ranked in
  as relevant and dropped only for time. Conflating them buries the handful
  that deserve attention under the tail that never did. Anything matching
  ``deferred-`` was not run here.
* Robust on partial trees: any listed path absent on disk is skipped (deletions),
  and the reverse-dep scan is skipped gracefully when ``backend/testing`` is
  absent.

Drift-sentinel surfaces
-----------------------
Some test files are *drift sentinels*: they AST/rglob-scan a whole source tree
(or a fixed source-file set) and diff it against a stored ``__fixtures__/*.json``
snapshot — ``test_websocket_emitter_inventory.py`` walks the entire
``backend/app`` tree, imports only ``backend/testing`` helpers, and never
references any ``app.<module>``. The dotted-import reverse-dep scan above can
NEVER select such a sentinel, so a drift-introducing change (a new emitter, a new
``event_type``) merges green on the task PR and only breaks at the promotion-PR
full matrix. To close that gap a committed manifest
(``backend/testing/drift_sentinel_surfaces.json``) maps a SURFACE GLOB
(``**`` = any depth, ``*`` = one path segment) to the sentinel test file(s) that
guard it; ``resolve_impacted`` selects a registered sentinel whenever a changed
path matches its surface glob (and the sentinel exists on disk). The manifest is
data-only and the load is fail-open: a missing / unreadable / malformed manifest
yields no sentinels rather than crashing the gate.
"""

import argparse
import ast
import json
import os
import re
import sys

# Maximum BFS depth for transitive importer expansion. The ``seen_mods`` dedup
# set already guarantees termination on cyclic graphs; this cap bounds scan cost
# on pathologically deep chains (e.g. a 20-layer abstraction stack).
MAX_HOPS = 6

# Repo root is two levels up from this file (backend/testing -> repo root).
_DEFAULT_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)

# A changed backend app module: capture the dotted-path remainder after
# ``backend/app/`` so ``backend/app/routers/agent.py`` -> ``routers/agent``.
_APP_PY_RE = re.compile(r"^backend/app/(.+)\.py$")
# The former merge-resolution lane selected only top-level app modules via this
# convention. Keep that coverage in the shared attributed resolver, rather than
# re-running the same test file through a raw exit-code lane.
_CONVENTIONAL_APP_PY_RE = re.compile(r"^backend/app/([A-Za-z0-9_]+)\.py$")
# A backend test file, by the pytest ``test_`` collection convention.
_TEST_PY_RE = re.compile(r"^backend/testing/test_[A-Za-z0-9_]+\.py$")

# The committed surface -> drift-sentinel manifest, relative to the repo root.
_SURFACE_MANIFEST_REL = "backend/testing/drift_sentinel_surfaces.json"


def app_module_for_path(repo_rel: str) -> "str | None":
    """Return the dotted module path for a ``backend/app/<...>.py`` path, else None.

    ``backend/app/routers/agent.py`` -> ``app.routers.agent``;
    ``backend/app/foo.py`` -> ``app.foo``. Non-app / non-``.py`` paths -> None.
    """
    m = _APP_PY_RE.match(repo_rel.strip())
    if not m:
        return None
    return "app." + m.group(1).replace("/", ".")


def module_reference_patterns(dotted: str) -> "list[re.Pattern]":
    """Compiled regexes that match a reference to ``dotted`` in test source.

    Two forms are covered:

    * The full dotted path with an identifier boundary on each end — catches
      ``import app.x.y``, ``from app.x.y import z``, ``patch("app.x.y.z")`` and
      ``patch.object(app.x.y, ...)``. The trailing boundary excludes a longer
      sibling (``app.foo`` must not match ``app.foobar``); the leading boundary
      (which also forbids a preceding ``.``) avoids matching a same-named
      attribute of an unrelated package (``otherpkg.app.foo``). A trailing ``.``
      is *allowed* so ``app.foo`` still matches ``app.foo.bar`` in a patch target.
    * ``from <parent> import <name>`` — this form never spells the dotted path
      contiguously (e.g. module ``app.routers.agent`` referenced as
      ``from app.routers import agent``), so it needs its own pattern with a
      boundary around the imported ``<name>``.
    """
    parts = dotted.split(".")
    esc = re.escape(dotted)
    pats = [
        re.compile(r"(?<![A-Za-z0-9_.])" + esc + r"(?![A-Za-z0-9_])"),
    ]
    if len(parts) >= 2:
        parent = re.escape(".".join(parts[:-1]))
        name = re.escape(parts[-1])
        pats.append(
            re.compile(
                r"from\s+"
                + parent
                + r"\s+import\s+[^\n]*?(?<![A-Za-z0-9_])"
                + name
                + r"(?![A-Za-z0-9_])"
            )
        )
    return pats


_DOTTED_APP_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(app(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
)
_FROM_APP_IMPORT_RE = re.compile(
    r"from\s+(app(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\s+([^\n]+)"
)


def _referenced_app_modules(content: str, known_modules) -> "set[str]":
    """Return known ``app.*`` modules referenced by one source-file read.

    Extract broad dotted references once and retain every known module prefix.
    This preserves the historical boundary semantics (``app.foo`` matches
    ``app.foo.bar`` but not ``app.foobar``) without rescanning the file for
    every frontier module. The explicit from-import pass preserves references
    such as ``from app.routers import agent``.
    """
    known = set(known_modules)
    referenced: "set[str]" = set()
    for match in _DOTTED_APP_REFERENCE_RE.finditer(content):
        parts = match.group(1).split(".")
        for end in range(2, len(parts) + 1):
            candidate = ".".join(parts[:end])
            if candidate in known:
                referenced.add(candidate)
    for match in _FROM_APP_IMPORT_RE.finditer(content):
        parent = match.group(1)
        if parent in known:
            referenced.add(parent)
        for raw_name in match.group(2).split(","):
            name = raw_name.strip().lstrip("(").rstrip(")")
            name = re.split(r"\s+as\s+", name, maxsplit=1)[0].strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            candidate = parent + "." + name
            if candidate in known:
                referenced.add(candidate)
    return referenced


# A string mock-patch target: ``patch("app.foo.bar", ...)``,
# ``mocker.patch('app.foo.baz', ...)`` or ``unittest.mock.patch(...)``. Capture
# the first dotted argument. ``patch.object(app.foo, ...)`` does not match
# (no quoted dotted target), and ``monkeypatch`` is not ``patch`` (the ``\b``
# requires a word boundary before ``patch``).
_PATCH_TARGET_RE = re.compile(
    r"""\b(?:unittest\.mock\.)?patch\s*\(\s*["']([A-Za-z0-9_.]+)["']"""
)


def _count_patch_targets(content: str, modules: "set[str]") -> int:
    """Count string mock-patch targets in ``content`` that name one of ``modules``.

    A test that string-patches ``app.<module>.<attr>`` is coupled to that
    module's runtime surface: a behavior/signature change in the module — or in
    one of its transitive importers, when the patch names a closure module —
    breaks the patch (and often the assertion right under it) silently. Used as
    the primary within-tier tiebreaker when ranking impacted tests by
    relevance, because a patched call-site is a stronger drift signal than a
    bare import.
    """
    count = 0
    for match in _PATCH_TARGET_RE.finditer(content):
        target = match.group(1)
        if target.startswith("backend."):
            target = target[len("backend.") :]
        if not target.startswith("app."):
            continue
        parts = target.split(".")
        for end in range(2, len(parts) + 1):
            if ".".join(parts[:end]) in modules:
                count += 1
                break
    return count


def _index_app_importers(
    repo_root: str, changed_modules
) -> "tuple[dict[str, set[str]], set[str]]":
    """Build ``referenced module -> direct importers`` in one app-tree scan."""
    app_dir = os.path.join(repo_root, "backend", "app")
    if not os.path.isdir(app_dir):
        return {}, set(changed_modules)
    sources: "dict[str, str]" = {}
    for dirpath, _dirs, filenames in os.walk(app_dir):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            abs_path = os.path.join(dirpath, fname)
            rel = os.path.relpath(abs_path, repo_root).replace(os.sep, "/")
            importer = app_module_for_path(rel)
            if importer is None:
                continue
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    sources[importer] = fh.read()
            except OSError:
                continue
    known_modules = set(sources) | set(changed_modules)
    reverse: "dict[str, set[str]]" = {}
    for importer, content in sources.items():
        for referenced in _referenced_app_modules(content, known_modules):
            if referenced != importer:
                reverse.setdefault(referenced, set()).add(importer)
    return reverse, known_modules


def _glob_to_regex(glob: str) -> "re.Pattern":
    """Compile a repo-relative surface glob to an anchored regex.

    Segment semantics match the manifest's documented convention:

    * ``**/`` matches zero or more path segments (any depth, including none), so
      ``backend/app/**/*.py`` matches both ``backend/app/foo.py`` and
      ``backend/app/routers/agent.py``.
    * a bare ``**`` matches anything, ``/`` included.
    * ``*`` matches a single path segment (no ``/``).
    * every other character is matched literally.
    """
    out: "list[str]" = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                if i + 2 < n and glob[i + 2] == "/":
                    out.append(r"(?:.*/)?")  # '**/' -> optional any-depth dirs
                    i += 3
                else:
                    out.append(r".*")  # bare '**'
                    i += 2
            else:
                out.append(r"[^/]*")  # single-segment '*'
                i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _surface_manifest_path(repo_root: str) -> str:
    """Where the drift-sentinel manifest lives for ``repo_root``.

    Docker test harness: the backend tree is copied with app/ and testing/ as
    siblings and no enclosing backend/ directory, so the dev-checkout-relative
    ``_SURFACE_MANIFEST_REL`` does not resolve. The manifest is co-located with
    this module; fall back to it -- but only for the DEFAULT root, so hermetic
    fixture roots stay strictly self-contained (a fixture without a manifest
    must read as no manifest, never as the repository's own).

    Shared by every manifest reader. A second reader that re-derived this path
    would work in a developer checkout and read nothing under the harness every
    backend CI shard uses -- passing while proving nothing, which is the
    vacuous-guard shape this file's own sentinels exist to catch.
    """
    manifest_path = os.path.join(repo_root, _SURFACE_MANIFEST_REL)
    if not os.path.isfile(manifest_path) and repo_root == _DEFAULT_REPO_ROOT:
        manifest_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "drift_sentinel_surfaces.json",
        )
    return manifest_path


def _load_surface_manifest(repo_root: str) -> "list[tuple[re.Pattern, list[str]]]":
    """Load and compile the drift-sentinel surface manifest (fail-open).

    Returns a list of ``(compiled_glob, [sentinel_rel_path, ...])`` pairs. Any
    problem — file absent, unreadable, invalid JSON, or a malformed/empty entry —
    yields no pairs rather than raising, so a broken manifest can never crash the
    merge gate (mirroring the module's "robust on partial trees" contract).
    """
    manifest_path = _surface_manifest_path(repo_root)
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    surfaces = data.get("surfaces")
    if not isinstance(surfaces, list):
        return []
    compiled: "list[tuple[re.Pattern, list[str]]]" = []
    for entry in surfaces:
        if not isinstance(entry, dict):
            continue
        glob = entry.get("glob")
        sentinels = entry.get("sentinels")
        if not isinstance(glob, str) or not glob:
            continue
        if not isinstance(sentinels, list):
            continue
        paths = [s for s in sentinels if isinstance(s, str) and s]
        if not paths:
            continue
        try:
            regex = _glob_to_regex(glob)
        except re.error:
            continue
        compiled.append((regex, paths))
    return compiled


def load_surface_manifest(repo_root: str) -> "list[tuple[re.Pattern, list[str]]]":
    """Public accessor for the compiled drift-sentinel surface manifest.

    Thin wrapper around :func:`_load_surface_manifest` so a non-testing
    production script (e.g. ``scripts/static_release_guard.py``) can reuse the
    same compiled ``(glob_regex, [sentinel_path, ...])`` pairs that drive the
    merge-time impacted-test selector, instead of re-deriving its own
    prefix/glob list that can silently drift out of sync with this manifest.
    """
    return _load_surface_manifest(repo_root)


def _is_backend_surface(glob: str) -> bool:
    """Whether a manifest surface glob lies entirely under ``backend/``.

    Compared on a path SEGMENT boundary, never as a bare string prefix, so
    ``backend-other/**`` is not mistaken for a backend surface.
    """
    return glob == "backend" or glob.startswith("backend/")


def corpus_lane_required_sentinels(repo_root: str) -> "list[str]":
    """Registered sentinels that the standalone corpus lane must carry.

    WHY THIS IS DERIVED RATHER THAN LISTED
    --------------------------------------
    The corpus lane (``scripts/test-backend.sh --standalone``) is the only lane
    that runs on a diff containing no backend Python file, so it is the only
    place a sentinel guarding ``admin-ui/``, ``agent/``, ``scripts/`` or a
    derived artifact can be reached. A sentinel every one of whose registered
    surfaces lies under ``backend/`` is in the opposite position: the only
    diffs that select it are backend diffs, and every backend diff already
    resolves selectors through the backend-authoring lane. Demanding a roster
    seat for it buys nothing.

    That distinction used to live as a hand-maintained four-name set inside
    ``scripts/test_guard_gate_left_shift.py``. It went stale the moment a new
    backend-only sentinel was registered without someone remembering to extend
    it -- which is how ``test_scripts_dir_resolution_guard.py`` (registered on
    ``backend/**/*.py``, and whose own manifest entry states in writing that it
    is deliberately kept OUT of the standalone lane because it reads the whole
    backend tree over a bind mount under a 30s timeout) turned a correct
    registration into a red promotion matrix.

    Reading it off the manifest closes that: to make a sentinel exempt you now
    have to take its non-backend surface away, which visibly shrinks what the
    sentinel guards and is reviewable at the line where it happens -- not here,
    and not in the test that consumes this.

    Fails CLOSED. A sentinel is dropped from the required set only on positive
    evidence that all of its surfaces are backend surfaces. An unreadable
    manifest yields no sentinels at all upstream, which callers must treat as
    the vacuous case rather than as "nothing required".
    """
    surfaces_by_sentinel: "dict[str, list[str]]" = {}
    try:
        with open(_surface_manifest_path(repo_root), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("surfaces")
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        glob = entry.get("glob")
        sentinels = entry.get("sentinels")
        if not isinstance(glob, str) or not glob:
            continue
        if not isinstance(sentinels, list):
            continue
        for sentinel in sentinels:
            if isinstance(sentinel, str) and sentinel:
                surfaces_by_sentinel.setdefault(sentinel, []).append(glob)
    return sorted(
        sentinel
        for sentinel, globs in surfaces_by_sentinel.items()
        if not all(_is_backend_surface(glob) for glob in globs)
    )


def sentinel_file(sentinel: str) -> str:
    """The on-disk file part of a registered sentinel selector.

    A sentinel may be registered either as a plain test file
    (``backend/testing/test_x.py``) or as a single pytest node id
    (``backend/testing/test_x.py::test_one``). The node-id form exists for a
    file whose OTHER tests need a database: registering the whole file would
    drag a DB-dependent module into the ``--no-postgres`` standalone lane,
    where it errors on a missing fixture rather than reporting drift. Every
    on-disk check below operates on this file part.
    """
    return sentinel.split("::", 1)[0]


def _sentinel_exists(sentinel: str, repo_root: str) -> bool:
    """Whether a registered sentinel exists on disk (harness-layout aware).

    Checks ``<repo_root>/<file part>`` first (dev checkout / CI workspace). For
    the DEFAULT root only, also checks the sentinel as a sibling of this module:
    the Docker test harness copies the backend tree with testing/ next to app/
    and no enclosing backend/ directory. Hermetic fixture roots keep the strict
    repo_root-only check.

    ``sentinel`` may carry a ``::node`` suffix; only its file part is probed,
    so a node-id registration is subject to the same existence discipline as a
    whole-file one and a vanished file still never selects.
    """
    rel = sentinel_file(sentinel)
    if os.path.isfile(os.path.join(repo_root, rel)):
        return True
    if repo_root == _DEFAULT_REPO_ROOT and rel.startswith("backend/testing/"):
        sibling = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            rel[len("backend/testing/"):],
        )
        return os.path.isfile(sibling)
    return False


def surface_sentinels(changed_paths, repo_root: str) -> "set[str]":
    """Public accessor for the drift sentinels a changed set selects.

    Companion to :func:`load_surface_manifest` for the same reason: a
    production script (``scripts/static_release_guard.py``) must be able to
    name the sentinels a change activates without re-implementing the
    glob-match + on-disk-existence discipline that drives the merge gate.
    """
    return _surface_sentinels(changed_paths, repo_root)


def _surface_sentinels(changed_paths, repo_root: str) -> "set[str]":
    """Drift sentinels whose surface glob matches any changed path (on-disk only).

    A registered sentinel is added only if it exists as a file under ``repo_root``,
    so a stale manifest entry (sentinel deleted/renamed) never selects a vanished
    file — the same existence discipline the reverse-dep scan applies.
    """
    manifest = _load_surface_manifest(repo_root)
    if not manifest:
        return set()
    selected: "set[str]" = set()
    for raw in changed_paths:
        path = raw.strip()
        if not path:
            continue
        for regex, sentinels in manifest:
            if not regex.match(path):
                continue
            for sentinel in sentinels:
                if sentinel in selected:
                    continue
                if _sentinel_exists(sentinel, repo_root):
                    selected.add(sentinel)
    return selected


def _rank_key(entry: "tuple[int, int, int, int, str]"):
    """Sort key for a ranked entry: ``(tier, -n_patch, -n_direct, -n_closure, path)``.

    Tier first (directly changed tests < import-hop tiers < drift sentinels),
    then relevance counts descending (mock-patch coupling, direct changed-module
    references, closure references), then path ascending for determinism.
    """
    tier, n_patch, n_direct, n_closure, path = entry
    return (tier, -n_patch, -n_direct, -n_closure, path)


def resolve_impacted(changed_paths, repo_root: str) -> "list[str]":
    """Compute the impacted ``backend/testing/test_*.py`` set, ranked by relevance.

    ``changed_paths`` is any iterable of repo-relative paths. ``repo_root`` roots
    the on-disk existence checks and the reverse-dep scan.

    The emitted order is ranked by relevance to the diff — NOT alphabetical — so
    that a bounded selection (the merge gate caps the run) keeps the tests most
    likely to catch a contract break on the changed surface:

    * tier -1: directly changed/added ``test_*.py`` files and conventional
      ``test_<module>.py`` files for directly changed top-level app modules;
    * tier 0..MAX_HOPS: test files that reference a closure module (a changed
      module or a transitive importer of one), ordered by the minimum import-hop
      distance from a changed module — hop 0 references a changed module
      directly, hop N references a module N import-hops away;
    * tier MAX_HOPS + 1: surface-glob drift sentinels (no import path at all).
    Within a tier, tests that mock-patch the closure surface (most patch targets
    first), reference more changed modules directly, or reference more closure
    modules rank first; ``path`` breaks remaining ties deterministically.
    """
    # Materialize once: ``changed_paths`` may be a single-use iterable, and it is
    # scanned both here and by the surface-glob sentinel pass below.
    changed_list = list(changed_paths)
    testing_dir = os.path.join(repo_root, "backend", "testing")
    initial_changed_modules: "list[str]" = []
    priority_tests: "set[str]" = set()

    for raw in changed_list:
        path = raw.strip()
        if not path:
            continue
        if _TEST_PY_RE.match(path):
            # A directly changed/added test file — include it (if it exists on
            # disk, so a deletion doesn't select a vanished file). It ranks at
            # tier -1, ahead of any reference-matched file.
            if os.path.isfile(os.path.join(repo_root, path)):
                priority_tests.add(path)
            continue
        conventional = _CONVENTIONAL_APP_PY_RE.match(path)
        if conventional:
            test_path = f"backend/testing/test_{conventional.group(1)}.py"
            if os.path.isfile(os.path.join(repo_root, test_path)):
                priority_tests.add(test_path)
        dotted = app_module_for_path(path)
        if dotted is not None:
            initial_changed_modules.append(dotted)

    ranked: "list[tuple[int, int, int, int, str]]" = []
    emitted: "set[str]" = set()
    for path in sorted(priority_tests):
        emitted.add(path)
        ranked.append((-1, 0, 0, 0, path))

    if initial_changed_modules and os.path.isdir(testing_dir):
        reverse_importers, known_modules = _index_app_importers(
            repo_root, initial_changed_modules
        )
        # Transitive closure (BFS, bounded by MAX_HOPS): app modules that
        # import a changed module — directly or indirectly — are also relevant.
        # ``depth`` records the hop distance from the nearest changed module at
        # first discovery (BFS first-visit = minimum), giving both dedup /
        # termination and the relevance tier for referencing tests.
        depth: "dict[str, int]" = {}
        frontier: "list[str]" = []
        for mod in initial_changed_modules:
            if mod not in depth:
                depth[mod] = 0
                frontier.append(mod)
        for hop in range(1, MAX_HOPS + 1):
            if not frontier:
                break
            next_frontier: "list[str]" = []
            for mod in frontier:
                for importer in reverse_importers.get(mod, ()):
                    if importer not in depth:
                        depth[importer] = hop
                        next_frontier.append(importer)
            frontier = next_frontier
        changed_set: "set[str]" = set(initial_changed_modules)
        closure = set(depth)
        for name in os.listdir(testing_dir):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            rel = "backend/testing/" + name
            if rel in emitted:
                continue
            abs_path = os.path.join(testing_dir, name)
            if not os.path.isfile(abs_path):
                continue
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                # Unreadable file: skip rather than crash the whole gate.
                continue
            referenced = _referenced_app_modules(content, known_modules)
            matching = referenced & closure
            if not matching:
                continue
            min_hop = min(depth[m] for m in matching)
            n_direct = len(matching & changed_set)
            n_closure = len(matching)
            n_patch = _count_patch_targets(content, closure)
            emitted.add(rel)
            ranked.append((min_hop, n_patch, n_direct, n_closure, rel))

    # Surface-glob drift sentinels: select whole-tree/fixed-set sentinels the
    # dotted-import scan structurally cannot reach. They have no import path, so
    # they rank at the last tier (MAX_HOPS + 1) unless a direct change or a
    # reference match already emitted the file.
    for sentinel in _surface_sentinels(changed_list, repo_root):
        if sentinel in emitted:
            continue
        # A node-id sentinel collapses into a whole-file selection the earlier
        # passes already made: running the file covers the node, and emitting
        # both would run the module twice in one gate invocation.
        if sentinel_file(sentinel) in emitted:
            continue
        emitted.add(sentinel)
        ranked.append((MAX_HOPS + 1, 0, 0, 0, sentinel))

    ranked.sort(key=_rank_key)
    return [entry[-1] for entry in ranked]


def resolve_impacted_partition(
    changed_paths, repo_root: str
) -> "tuple[list[str], list[str]]":
    """Return ``(ordinary, mandatory)`` impacted selectors.

    ``resolve_impacted`` remains the compatibility API and preserves its
    relevance order, including sentinels at the end. The merge-time gate uses
    this partition so its eight-selector budget applies only to ordinary
    relevance-ranked tests; every on-disk sentinel selected by the committed
    surface manifest is mandatory and retained exactly once.
    """
    changed_list = list(changed_paths)
    ranked = resolve_impacted(changed_list, repo_root)
    ranked_set = set(ranked)
    mandatory_set: "set[str]" = set()
    for sentinel in _surface_sentinels(changed_list, repo_root):
        # A node-id sentinel that collapsed into its whole-file selection is
        # still mandatory: carry the form that actually appears in ``ranked``
        # so the collapse cannot demote a sentinel into the capped budget.
        mandatory_set.add(
            sentinel if sentinel in ranked_set else sentinel_file(sentinel)
        )
    ordinary = [path for path in ranked if path not in mandatory_set]
    mandatory = [path for path in ranked if path in mandatory_set]
    return ordinary, mandatory


def load_test_file_costs(durations_path: str) -> "dict[str, float]":
    """Return ``{repo-relative test file: measured seconds}``.

    Reads a pytest-split ``.test_durations`` file, whose keys are node ids
    relative to the pytest rootdir (``backend/``) — ``testing/test_x.py::test_y``
    — and sums each file's nodes.

    FAIL-OPEN, deliberately. A missing, unreadable or malformed durations file
    yields ``{}``, and ``apply_cost_budget`` then declines to budget at all. The
    alternative — treating "no measurements" as "everything is free" or
    "everything costs the maximum" — would either not bound the gate or select
    nothing, and selecting nothing is the vacuous-guard failure this module
    exists to avoid.
    """
    try:
        with open(durations_path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    costs: "dict[str, float]" = {}
    for node_id, seconds in raw.items():
        if not isinstance(node_id, str):
            continue
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            continue
        rel = "backend/" + node_id.split("::", 1)[0]
        costs[rel] = costs.get(rel, 0.0) + value
    return costs


def apply_cost_budget(
    ordinary,
    costs: "dict[str, float]",
    budget_seconds,
    reserved_seconds: float = 0.0,
) -> "tuple[list[str], list[str], float]":
    """Return ``(kept, deferred, kept_seconds)`` for a wall-clock budget.

    Why this exists at all: a selector-COUNT cap cannot bound runtime. Measured
    against the committed estate, the eight heaviest test files sum to ~42
    minutes — above any budget a merge gate can be given. The gate's own
    timeout would then fire on healthy work and the surrounding task would be
    cascade-killed by the CI backstop. That is the harness-budget-inversion
    class, and a count cap walks straight into it.

    Items are taken in relevance order while budget remains, and one that does
    not fit is SKIPPED rather than terminating the walk, so a single expensive
    high-rank file cannot starve every cheaper file behind it.

    A file with no measurement is charged the mean of the measured files rather
    than zero: an unmeasured file is usually a NEW one, and charging it nothing
    would let an unbounded number of them past the budget.
    """
    ordinary = list(ordinary)
    if not costs or not budget_seconds or budget_seconds <= 0:
        return ordinary, [], 0.0

    unknown_cost = sum(costs.values()) / len(costs)
    remaining = float(budget_seconds) - reserved_seconds
    kept: "list[str]" = []
    deferred: "list[str]" = []
    spent = 0.0
    for rel in ordinary:
        cost = costs.get(rel, unknown_cost)
        if kept and cost > remaining:
            deferred.append(rel)
            continue
        # The first selector is kept even if it alone exceeds the budget:
        # running the single most relevant test slowly beats running no test at
        # all, and a gate that can select nothing is exactly the failure mode
        # this module is built to prevent.
        kept.append(rel)
        remaining -= cost
        spent += cost
    return kept, deferred, spent


def collect_first_party_patch_targets(testing_dir: str) -> "list[str]":
    """Return sorted, deduplicated first-party mock.patch() string targets from tests.

    Scans every ``test_*.py`` under *testing_dir* using AST to find
    ``patch("app.*")`` calls. Excludes calls that carry ``create=True`` (those
    intentionally name attributes that don't exist at module level). Returns the
    unique targets sorted alphabetically, normalized to the ``app.*`` form (the
    ``backend.`` prefix is stripped).

    Used by the merge-gate invariant ``test_mock_patch_targets_resolve`` to
    verify that every patched attribute still exists in its host module at PR time
    rather than only at promotion-PR full-matrix time.
    """
    targets: "set[str]" = set()
    if not os.path.isdir(testing_dir):
        return []
    for fname in os.listdir(testing_dir):
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        fpath = os.path.join(testing_dir, fname)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=fname)
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_patch = (isinstance(func, ast.Attribute) and func.attr == "patch") or (
                isinstance(func, ast.Name) and func.id == "patch"
            )
            if not is_patch:
                continue
            if any(
                kw.arg == "create"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Constant) or not isinstance(
                first_arg.value, str
            ):
                continue
            raw = first_arg.value
            normalized = raw[len("backend."):] if raw.startswith("backend.") else raw
            if normalized.startswith("app.") and "." in normalized[4:]:
                targets.add(normalized)
    return sorted(targets)


# Between two adjacent tokens of a Python call, the grammar admits only
# whitespace (newlines included, inside the call's parentheses), a
# backslash line-continuation, and comments. So a file whose text does not
# match this cannot tokenize to NAME(create) OP(=) NAME(True), and therefore
# cannot produce the ``keyword(arg='create', value=Constant(True))`` node the
# scan below looks for. That makes the prefilter sound by construction rather
# than by inspection -- see
# ``test_create_true_prefilter_admits_every_spelling_the_ast_scan_accepts``,
# which pins each spelling this must not reject.
#
# ``**{"create": True}`` needs no coverage: dict-unpacking produces
# ``keyword(arg=None)``, which the AST scan already declines to match.
#
# The alternation branches start with disjoint characters ("#" is not in
# ``[\s\\]``) and the group is atomic, so there is no backtracking blowup on
# the long non-matching runs that dominate a real source tree; measured at
# 0.002s against an adversarial 40KB input.
_CREATE_TRUE_TOKEN_GAP = r"(?>[\s\\]|\#[^\n]*)*"
_CREATE_TRUE_HINT = re.compile(
    "create" + _CREATE_TRUE_TOKEN_GAP + "=" + _CREATE_TRUE_TOKEN_GAP + "True"
)


def collect_create_true_patch_targets(testing_dir: str) -> "list[tuple[str, str]]":
    """Return sorted ``(test_file, target)`` pairs for ``create=True`` patch calls.

    The inverse of :func:`collect_first_party_patch_targets`: scans every
    ``test_*.py`` under *testing_dir* using AST for ``patch("app.*", ...,
    create=True)`` calls. ``create=True`` tells mock to fabricate an attribute it
    deems absent at module level; once the real attribute exists the flag is a
    no-op that silently suppresses the ``AttributeError`` a later rename/removal
    of that symbol would raise — voiding the drift protection the string-target
    invariant provides (which itself skips ``create=True`` patches).

    Used by the merge-gate invariant ``test_redundant_create_true_patches_flagged``
    to flag redundant ``create=True`` whose target now resolves at module top
    level. Targets are normalized to ``app.*`` form (the ``backend.`` prefix is
    stripped); pairs are deduplicated and sorted.
    """
    pairs: "set[tuple[str, str]]" = set()
    if not os.path.isdir(testing_dir):
        return []
    for fname in os.listdir(testing_dir):
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        fpath = os.path.join(testing_dir, fname)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            # Cheap textual prefilter before the expensive parse. Reading the
            # whole ``backend/testing`` tree costs 0.16s; AST-parsing all of it
            # costs 10.77s, and only 9 of 627 files can contribute a pair at
            # all. Skipping the parse for the other 618 takes this scan from
            # 10.93s to 0.74s (14.8x) on the current checkout.
            #
            # This is a false-failure fix, not a nicety. The standalone lane
            # runs with ``--timeout=30``; over the Docker-on-Windows bind mount
            # the 10.9s native scan crosses it, and the guard then dies on a
            # timeout rather than its assertion -- reporting "slow filesystem"
            # while claiming to report a redundant-patch defect.
            if not _CREATE_TRUE_HINT.search(source):
                continue
            tree = ast.parse(source, filename=fname)
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_patch = (isinstance(func, ast.Attribute) and func.attr == "patch") or (
                isinstance(func, ast.Name) and func.id == "patch"
            )
            if not is_patch:
                continue
            if not any(
                kw.arg == "create"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Constant) or not isinstance(
                first_arg.value, str
            ):
                continue
            raw = first_arg.value
            normalized = raw[len("backend."):] if raw.startswith("backend.") else raw
            if normalized.startswith("app.") and "." in normalized[4:]:
                pairs.add((fname, normalized))
    return sorted(pairs)


def _read_changed(paths_argv, stdin) -> "list[str]":
    """Positional args if any, else the newline-delimited STDIN payload."""
    if paths_argv:
        return list(paths_argv)
    # Only read stdin when it isn't an interactive terminal, so an accidental
    # bare invocation doesn't hang waiting for input.
    if getattr(stdin, "isatty", lambda: False)():
        return []
    return stdin.read().splitlines()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve the backend test files impacted by a set of changed repo "
            "paths (reverse-dependency scan). Changed paths on STDIN (one per "
            "line) or as positional args; impacted test files to STDOUT."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="changed repo-relative paths (default: read newline-delimited STDIN)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the emitted count; overflow is logged to STDERR (never dropped)",
    )
    parser.add_argument(
        "--mandatory-output",
        dest="mandatory_output",
        default=None,
        help=(
            "write matched surface sentinels to this file; --limit applies "
            "only to ordinary selectors"
        ),
    )
    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        default=None,
        help="repo root used to resolve on-disk paths (default: inferred)",
    )
    parser.add_argument(
        "--budget-seconds",
        dest="budget_seconds",
        type=float,
        default=None,
        help=(
            "cap the emitted set by SUMMED MEASURED COST as well as by count; "
            "overflow is logged to STDERR (never dropped). A count cap alone "
            "cannot bound runtime -- the eight heaviest files in the estate "
            "are ~42 minutes"
        ),
    )
    parser.add_argument(
        "--durations",
        dest="durations",
        default=None,
        help=(
            "pytest-split durations file backing --budget-seconds "
            "(default: <repo-root>/backend/.test_durations)"
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root if args.repo_root is not None else _DEFAULT_REPO_ROOT
    changed = _read_changed(args.paths, sys.stdin)
    mandatory: "list[str]" = []
    if args.mandatory_output is not None:
        ordinary, mandatory = resolve_impacted_partition(changed, repo_root)
        try:
            with open(
                args.mandatory_output, "w", encoding="utf-8", newline=""
            ) as fh:
                for rel in mandatory:
                    fh.write(rel + "\n")
        except OSError as exc:
            print(
                f"mandatory selector output failed: {args.mandatory_output}: {exc}",
                file=sys.stderr,
            )
            return 2
        impacted = ordinary
    else:
        impacted = resolve_impacted(changed, repo_root)

    if args.limit is not None and args.limit >= 0 and len(impacted) > args.limit:
        overflow = impacted[args.limit :]
        impacted = impacted[: args.limit]
        for rel in overflow:
            print(f"deferred-rank: {rel}", file=sys.stderr)
        # A count for consumers that summarise rather than list. A change to a
        # core module puts HUNDREDS of files in this tail, so a reader needs the
        # magnitude without the roll.
        print(
            f"rank-cap: {len(overflow)} impacted file(s) beyond --limit "
            f"{args.limit} deferred",
            file=sys.stderr,
        )

    if args.budget_seconds is not None:
        durations_path = args.durations or os.path.join(
            repo_root, "backend", ".test_durations"
        )
        costs = load_test_file_costs(durations_path)
        if not costs:
            # Say so. "Budgeting was requested and did not happen" and
            # "budgeting happened and nothing was dropped" produce identical
            # STDOUT, and a caller that sized its timeout around the budget
            # needs to know which one it got.
            print(
                "budget: no usable measurements in "
                f"{durations_path} — cost budget NOT applied",
                file=sys.stderr,
            )
        else:
            # Mandatory sentinels are never dropped (they are the surface-drift
            # contract, and they are cheap -- ~6s for a typical match), but
            # they do consume wall clock, so charge them against the budget
            # rather than pretending the gate is only as slow as its ordinary
            # selectors.
            reserved = sum(costs.get(rel, 0.0) for rel in mandatory)
            impacted, budget_overflow, spent = apply_cost_budget(
                impacted, costs, args.budget_seconds, reserved
            )
            for rel in budget_overflow:
                print(f"deferred-budget: {rel}", file=sys.stderr)
            print(
                f"budget: {spent + reserved:.0f}s of {args.budget_seconds:.0f}s "
                f"({len(impacted)} ordinary + {len(mandatory)} mandatory, "
                f"{len(budget_overflow)} deferred)",
                file=sys.stderr,
            )

    for rel in impacted:
        print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
