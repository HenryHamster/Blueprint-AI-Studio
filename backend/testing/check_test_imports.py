#!/usr/bin/env python3
"""Static staleness guard for ``from app.…`` imports written inside backend tests.

Why this guard exists
---------------------
PR #17578 split ``backend/app/release_qualifier_sweep.py`` and moved the
constant ``_AUTOMAIN_HEAD_FIRST_SEEN_KEY`` into a new module,
``backend/app/release_qualifier_debounce.py``.  It updated most call sites but
missed ``backend/testing/test_release_qualifier__trigger_and_dispatch_regressions.py``,
which kept importing the constant from the old module in three places.  That
``ImportError`` sat red on ``automain`` for four days and only surfaced at the
``automain`` -> ``main`` promotion PR, where it failed four tests across three
CI shards and blocked a release.

Two properties made it invisible, and both are permanent:

* ``automain`` pull requests carry **zero** CI checks *by design* -- ``ci.yml``'s
  ``pull_request`` trigger is ``branches: [main]`` only, and the deterministic
  gate is the promotion PR.  Nothing between the merge and the promotion runs
  these tests, so "CI will catch it" means "the release will catch it, days
  later".
* All three broken imports were **function-local** (``from app.X import Y``
  written inside a test function body), so ``pytest --collect-only`` imports the
  test module without error and reports a clean collection.  A
  collection-integrity check cannot see this class *at all*: the import
  statement does not execute until the test body runs.

So the only cheap place to catch the class is a static walk that reads *every*
import statement at *any* nesting depth -- function bodies, class bodies,
``try:`` blocks, ``if`` blocks -- and resolves it against the app tree on disk.
This checker is that walk, and it is wired into the standalone (no-DB) lane so
it runs on every path into ``automain`` instead of only at the release gate.

Nothing here imports an ``app`` module.  Those modules pull in SQLAlchemy, the
settings object and a database engine; the standalone lane runs with no
Postgres and ``--noconftest``.  Every module's export set is recovered by
parsing it with ``ast`` instead.

Fail-closed on ambiguity means SKIP, not report
-----------------------------------------------
A false positive here blocks every path into ``automain``, which is strictly
worse than the defect it guards against.  The resolver is therefore
deliberately conservative: anything it cannot decide **statically** is skipped,
never reported.  A target module is exempted from name checking entirely when
it uses ``from … import *``, defines a module-level ``__getattr__``, or reaches
for ``globals()``/``exec()``/``setattr()`` at module scope -- in each of those
cases the real export set is not knowable from the syntax tree.  The cost of a
skip is one missed catch; the cost of a bad report is a wedged pipeline.

Usage::

    python3 backend/testing/check_test_imports.py            # whole corpus
    python3 backend/testing/check_test_imports.py testing/test_foo.py
    python3 backend/testing/check_test_imports.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# Node types that open a NEW scope.  Names bound inside them are not module
# attributes, so the module-scope walk must not descend into their bodies --
# otherwise a local variable in some helper would masquerade as an export and
# turn a real stale import into a silent pass.
_NESTED_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

# Module-scope calls to any of these mean the module can mint attributes the
# syntax tree does not show.  Seeing one makes the module opaque: its imports
# are still checked for module existence, but never for name existence.
_DYNAMIC_BINDING_CALLS = frozenset({"globals", "vars", "exec", "eval", "setattr"})

# Context managers under which an import is *expected* to blow up, so a failing
# import there is the assertion rather than a defect.  ``test_invariants.py``
# genuinely pins ``with pytest.raises(ImportError): from app.release_scoring
# import AUTHORIZED_TEST_DELETIONS`` to prove a retired allowlist stays retired
# -- flagging that would make this guard un-landable.
_FAILURE_TOLERATING_CONTEXTS = frozenset(
    {"raises", "assertRaises", "assertRaisesRegex", "suppress", "xfail"}
)

_APP_PACKAGE = "app"


@dataclass(frozen=True)
class ModuleFacts:
    """What a statically parsed app module provably exports.

    ``opaque_reason`` is non-empty when the module's export set cannot be known
    from source (star imports, a module-level ``__getattr__``, dynamic
    ``globals()`` writes).  Callers must treat an opaque module as exporting
    everything rather than reporting a name against it.
    """

    names: frozenset[str]
    opaque_reason: str | None


@dataclass(frozen=True)
class Violation:
    """One import in a test file that cannot resolve against the app tree.

    ``kind`` is ``unknown-module`` (``backend/app/<mod>.py`` and the package
    ``__init__.py`` are both absent) or ``stale-name`` (the module exists but
    does not define the imported name).  ``statement`` is the reconstructed
    import, so the message is actionable without opening the file.
    """

    kind: str
    test_path: str
    lineno: int
    statement: str
    detail: str

    def format(self) -> str:
        return (
            f"[{self.kind}] {self.test_path}:{self.lineno}: {self.statement}\n"
            f"    {self.detail}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "test_path": self.test_path,
            "lineno": self.lineno,
            "statement": self.statement,
            "detail": self.detail,
        }


def repo_root() -> Path:
    """Repo root inferred from this file's location (``backend/testing/``)."""
    return Path(__file__).resolve().parents[2]


def _bind_target_names(target: ast.AST, out: set[str]) -> None:
    """Record the plain names an assignment/loop/``with`` target binds.

    ``Attribute`` and ``Subscript`` targets (``obj.x = 1``, ``d["k"] = 1``) bind
    no new module name and are intentionally ignored.
    """
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _bind_target_names(element, out)
    elif isinstance(target, ast.Starred):
        _bind_target_names(target.value, out)


def _walk_module_scope(node: ast.AST):
    """Yield every node that executes in module scope, skipping nested scopes.

    ``ast.walk`` is wrong here: it would descend into function and class bodies
    and collect their locals as if they were module attributes.  Descending into
    ``try``/``if``/``for``/``with`` bodies is required though -- a name bound
    under ``if TYPE_CHECKING:`` or in an ``except ImportError:`` fallback is a
    real module attribute.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if isinstance(child, _NESTED_SCOPE_NODES):
            continue
        yield from _walk_module_scope(child)


def _parse(path: Path) -> ast.Module | None:
    """Parse a Python file, returning ``None`` when it cannot be read/parsed."""
    try:
        source = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


@cache
def _module_facts_cached(path_str: str, _mtime_ns: int, _size: int) -> ModuleFacts:
    path = Path(path_str)
    tree = _parse(path)
    if tree is None:
        return ModuleFacts(frozenset(), "module source is unreadable or unparseable")

    names: set[str] = set()
    opaque_reason: str | None = None

    for node in _walk_module_scope(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if node.name == "__getattr__":
                opaque_reason = "module defines a module-level __getattr__"
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _bind_target_names(target, names)
        elif isinstance(
            node,
            (ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor),
        ):
            # Every one of these binds through a single `.target` node.
            _bind_target_names(node.target, names)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _bind_target_names(item.optional_vars, names)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star_source = node.module or "."
                    opaque_reason = (
                        f"module re-exports via `from {star_source} import *`"
                    )
                    continue
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _DYNAMIC_BINDING_CALLS:
                opaque_reason = (
                    f"module binds attributes dynamically via `{func.id}(...)`"
                )

    return ModuleFacts(frozenset(names), opaque_reason)


def module_facts(path: Path) -> ModuleFacts:
    """Statically recovered export set for one app module (mtime-keyed cache)."""
    try:
        stat = path.stat()
    except OSError:
        return ModuleFacts(frozenset(), "module source is unreadable or unparseable")
    return _module_facts_cached(str(path), stat.st_mtime_ns, stat.st_size)


def resolve_app_module(app_root: Path, dotted: str) -> Path | None:
    """Map ``app.a.b`` to ``<app_root>/a/b.py`` or ``<app_root>/a/b/__init__.py``.

    Returns ``None`` when neither exists.  ``app`` itself resolves to the
    package ``__init__.py``.  Cached on the (root, dotted) pair: the corpus
    issues thousands of imports against a few hundred modules, and on Windows
    the stat calls dominate the whole run.
    """
    return _resolve_app_module_cached(str(app_root), dotted)


@cache
def _resolve_app_module_cached(app_root_str: str, dotted: str) -> Path | None:
    app_root = Path(app_root_str)
    parts = dotted.split(".")
    if not parts or parts[0] != _APP_PACKAGE:
        return None
    tail = parts[1:]
    if not tail:
        init = app_root / "__init__.py"
        return init if init.is_file() else None
    if any(not part.isidentifier() for part in tail):
        return None
    candidate = app_root.joinpath(*tail)
    as_module = candidate.parent / (candidate.name + ".py")
    if as_module.is_file():
        return as_module
    as_package = candidate / "__init__.py"
    if as_package.is_file():
        return as_package
    return None


def _is_app_module(dotted: str | None) -> bool:
    return bool(dotted) and (
        dotted == _APP_PACKAGE or dotted.startswith(_APP_PACKAGE + ".")
    )


def _render_from_import(module: str, aliases: list[ast.alias]) -> str:
    rendered = ", ".join(
        alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"
        for alias in aliases
    )
    return f"from {module} import {rendered}"


def _expected_paths(app_root: Path, dotted: str, base: Path | None) -> str:
    """The on-disk paths a dotted app module could have occupied."""
    tail = dotted.split(".")[1:]
    if not tail:
        return _display(app_root / "__init__.py", base)
    candidate = app_root.joinpath(*tail)
    return (
        f"{_display(candidate.parent / (candidate.name + '.py'), base)} or "
        f"{_display(candidate / '__init__.py', base)}"
    )


def _display(path: Path, base: Path | None) -> str:
    root = base if base is not None else repo_root()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _context_manager_tail(expr: ast.expr) -> str | None:
    """Last dotted component of a ``with`` item's callee, e.g. ``pytest.raises``."""
    if not isinstance(expr, ast.Call):
        return None
    func = expr.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _tolerates_import_failure(node: ast.AST) -> bool:
    """True when a failing import inside ``node``'s body is the point of the test.

    ``try:`` with any handler covers the optional-dependency idiom; a ``with``
    on ``pytest.raises``/``contextlib.suppress``/``assertRaises`` covers the
    "this symbol must stay gone" idiom.  Both are deliberately broad: a missed
    catch costs one stale import, a false report wedges every merge.
    """
    if isinstance(node, ast.Try):
        return bool(node.handlers)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return any(
            _context_manager_tail(item.context_expr) in _FAILURE_TOLERATING_CONTEXTS
            for item in node.items
        )
    return False


def _collect_imports(
    node: ast.AST, tolerated: bool, out: list[ast.Import | ast.ImportFrom]
) -> None:
    """Gather every import statement at every nesting depth.

    This is the crux of the guard.  The #17578 imports were written *inside*
    test function bodies, so a module-body-only scan (and ``--collect-only``)
    sees nothing wrong.  Recursion here reaches function bodies, class bodies,
    ``try:`` blocks and ``if`` blocks alike, while threading a ``tolerated``
    flag so imports that are *supposed* to fail are not collected.
    """
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        if not tolerated:
            out.append(node)
        return

    if isinstance(node, ast.Try):
        guarded = tolerated or _tolerates_import_failure(node)
        for stmt in [*node.body, *node.orelse]:
            _collect_imports(stmt, guarded, out)
        for handler in node.handlers:
            for stmt in handler.body:
                _collect_imports(stmt, tolerated, out)
        for stmt in node.finalbody:
            _collect_imports(stmt, tolerated, out)
        return

    if isinstance(node, (ast.With, ast.AsyncWith)):
        guarded = tolerated or _tolerates_import_failure(node)
        for stmt in node.body:
            _collect_imports(stmt, guarded, out)
        return

    for child in ast.iter_child_nodes(node):
        _collect_imports(child, tolerated, out)


def check_test_file(
    path: Path,
    app_root: Path,
    *,
    display_path: str | None = None,
    base: Path | None = None,
) -> list[Violation]:
    """Resolve every ``app`` import in one test file against ``app_root``.

    Walks all nesting depths on purpose: the historical defect this guard exists
    for (#17578) was three *function-local* imports, which a module-body-only
    scan would miss entirely.  Relative imports, ``import *`` and imports whose
    failure is itself asserted are skipped rather than guessed at.
    """
    label = display_path if display_path is not None else _display(path, base)
    tree = _parse(path)
    if tree is None:
        # A test file that will not parse is pytest's failure to report, not
        # this guard's: duplicating it here would add noise, and a file this
        # checker cannot read is exactly the ambiguity it must not guess at.
        return []

    statements: list[ast.Import | ast.ImportFrom] = []
    _collect_imports(tree, False, statements)

    violations: list[Violation] = []
    for node in statements:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_app_module(alias.name):
                    continue
                if resolve_app_module(app_root, alias.name) is None:
                    violations.append(
                        Violation(
                            kind="unknown-module",
                            test_path=label,
                            lineno=node.lineno,
                            statement=f"import {alias.name}",
                            detail=(
                                f"no module `{alias.name}` on disk (looked for "
                                f"{_expected_paths(app_root, alias.name, base)})"
                            ),
                        )
                    )
            continue

        if not isinstance(node, ast.ImportFrom):
            continue
        # A relative import inside backend/testing does not target the app
        # package; leave it alone.
        if node.level:
            continue
        module = node.module
        if not _is_app_module(module) or module is None:
            continue

        target = resolve_app_module(app_root, module)
        if target is None:
            violations.append(
                Violation(
                    kind="unknown-module",
                    test_path=label,
                    lineno=node.lineno,
                    statement=_render_from_import(module, list(node.names)),
                    detail=(
                        f"no module `{module}` on disk (looked for "
                        f"{_expected_paths(app_root, module, base)})"
                    ),
                )
            )
            continue

        facts = module_facts(target)
        if facts.opaque_reason:
            continue
        is_package = target.name == "__init__.py"
        for alias in node.names:
            if alias.name == "*":
                continue
            if alias.name in facts.names:
                continue
            if is_package and resolve_app_module(app_root, f"{module}.{alias.name}"):
                # ``from app import shared_state`` / ``from app.routers import x``:
                # a submodule of a package, not an attribute of its __init__.
                continue
            violations.append(
                Violation(
                    kind="stale-name",
                    test_path=label,
                    lineno=node.lineno,
                    statement=_render_from_import(module, [alias]),
                    detail=(
                        f"`{alias.name}` is not defined by "
                        f"{_display(target, base)} (no top-level "
                        "def/class/assignment and no re-export). It was most "
                        "likely moved or renamed -- import it from the module "
                        "that owns it now."
                    ),
                )
            )
    return violations


def iter_test_files(testing_root: Path) -> list[Path]:
    """Every Python file under ``backend/testing``, sorted for stable output."""
    return sorted(p for p in testing_root.rglob("*.py") if p.is_file())


def find_stale_test_imports(
    *,
    root: Path | None = None,
    paths: list[Path] | None = None,
) -> list[Violation]:
    """Check the whole ``backend/testing`` corpus (or an explicit file list)."""
    base = (root or repo_root()).resolve()
    app_root = base / "backend" / "app"
    testing_root = base / "backend" / "testing"
    targets = paths if paths is not None else iter_test_files(testing_root)

    violations: list[Violation] = []
    for path in targets:
        resolved = Path(path).resolve()
        violations.extend(check_test_file(resolved, app_root, base=base))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Statically resolve every `from app.…` import in backend/testing "
            "against the app tree on disk. Catches imports left stale by a "
            "module split or rename, including the function-local ones that "
            "pytest collection cannot see (see PR #17578)."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Specific test files to check (default: all of backend/testing).",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (default: inferred from this file's location).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit violations as JSON instead of human-readable text.",
    )
    args = parser.parse_args(argv)

    base = Path(args.repo_root).resolve() if args.repo_root else repo_root()
    explicit = [Path(p) for p in args.paths] if args.paths else None
    violations = find_stale_test_imports(root=base, paths=explicit)

    if args.json:
        print(json.dumps([v.as_dict() for v in violations], indent=2, sort_keys=True))
        return 1 if violations else 0

    if not violations:
        checked = (
            len(explicit)
            if explicit is not None
            else len(iter_test_files(base / "backend" / "testing"))
        )
        print(f"All `app` imports in {checked} test file(s) resolve.")
        return 0

    for violation in violations:
        print(violation.format())
    print(
        f"\n{len(violations)} stale test import(s). Each one is an ImportError "
        "waiting for the promotion PR: point the import at the module that "
        "actually owns the symbol."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
