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
  N (most relevant); the overflow is reported to STDERR as
  ``deferred-unverified:`` lines (never silently dropped).
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


def _load_surface_manifest(repo_root: str) -> "list[tuple[re.Pattern, list[str]]]":
    """Load and compile the drift-sentinel surface manifest (fail-open).

    Returns a list of ``(compiled_glob, [sentinel_rel_path, ...])`` pairs. Any
    problem — file absent, unreadable, invalid JSON, or a malformed/empty entry —
    yields no pairs rather than raising, so a broken manifest can never crash the
    merge gate (mirroring the module's "robust on partial trees" contract).
    """
    manifest_path = os.path.join(repo_root, _SURFACE_MANIFEST_REL)
    if not os.path.isfile(manifest_path) and repo_root == _DEFAULT_REPO_ROOT:
        # Docker test harness: the backend tree is copied with app/ and
        # testing/ as siblings and no enclosing backend/ directory, so the
        # dev-checkout-relative _SURFACE_MANIFEST_REL does not resolve. The
        # manifest is co-located with this module; fall back to it — but only
        # for the DEFAULT root, so hermetic fixture roots stay strictly
        # self-contained (a fixture without a manifest must compile to []).
        manifest_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "drift_sentinel_surfaces.json",
        )
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


def _sentinel_exists(sentinel: str, repo_root: str) -> bool:
    """Whether a registered sentinel exists on disk (harness-layout aware).

    Checks ``<repo_root>/<sentinel>`` first (dev checkout / CI workspace). For
    the DEFAULT root only, also checks the sentinel as a sibling of this module:
    the Docker test harness copies the backend tree with testing/ next to app/
    and no enclosing backend/ directory. Hermetic fixture roots keep the strict
    repo_root-only check.
    """
    if os.path.isfile(os.path.join(repo_root, sentinel)):
        return True
    if repo_root == _DEFAULT_REPO_ROOT and sentinel.startswith("backend/testing/"):
        sibling = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            sentinel[len("backend/testing/"):],
        )
        return os.path.isfile(sibling)
    return False


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

    * tier -1: directly changed/added ``test_*.py`` files (the PR's own tests);
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
    directly_changed_tests: "list[str]" = []

    for raw in changed_list:
        path = raw.strip()
        if not path:
            continue
        if _TEST_PY_RE.match(path):
            # A directly changed/added test file — include it (if it exists on
            # disk, so a deletion doesn't select a vanished file). It ranks at
            # tier -1, ahead of any reference-matched file.
            if os.path.isfile(os.path.join(repo_root, path)):
                directly_changed_tests.append(path)
            continue
        dotted = app_module_for_path(path)
        if dotted is not None:
            initial_changed_modules.append(dotted)

    ranked: "list[tuple[int, int, int, int, str]]" = []
    emitted: "set[str]" = set()
    for path in directly_changed_tests:
        if path in emitted:
            continue
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
        emitted.add(sentinel)
        ranked.append((MAX_HOPS + 1, 0, 0, 0, sentinel))

    ranked.sort(key=_rank_key)
    return [entry[-1] for entry in ranked]


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
        "--repo-root",
        dest="repo_root",
        default=None,
        help="repo root used to resolve on-disk paths (default: inferred)",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root if args.repo_root is not None else _DEFAULT_REPO_ROOT
    changed = _read_changed(args.paths, sys.stdin)
    impacted = resolve_impacted(changed, repo_root)

    if args.limit is not None and args.limit >= 0 and len(impacted) > args.limit:
        overflow = impacted[args.limit :]
        impacted = impacted[: args.limit]
        for rel in overflow:
            print(f"deferred-unverified: {rel}", file=sys.stderr)

    for rel in impacted:
        print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
