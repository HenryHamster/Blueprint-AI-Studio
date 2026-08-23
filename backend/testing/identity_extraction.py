"""Static, collector-faithful Python test identity extraction.

The duration tools and conversational Workday floor need one conservative
definition of a test identity.  This module intentionally models only
top-level test functions and classes: nested classes are not pytest
collection roots.  It additionally follows module-local ``unittest.TestCase``
inheritance so test classes need not be named ``Test*`` to be counted, and
expands test methods a collectable class inherits from module-local bases --
``unittest.TestLoader().getTestCaseNames`` reports those under the *subclass*
name, so a subclass-only body is not an empty identity set.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


def extract_python_test_ids(source: str) -> set[str]:
    """Return static pytest/unittest node IDs from Python test source.

    Identities are bare function names for top-level ``test_*`` functions and
    ``ClassName::method`` for test methods.  A top-level class is collectable
    when its name follows pytest's configured ``Test``/``*Test`` convention,
    or when it is a direct or module-local indirect ``unittest.TestCase``
    subclass.  A collectable class carries the ``test_*`` methods it inherits
    from module-local bases under its own name, matching the collector.  Alias
    and cross-module inheritance cannot be proven statically and are
    deliberately left to the live collector.
    """
    return set(extract_python_test_nodes(ast.parse(source)))


def extract_python_test_ids_from_file(path: Path) -> set[str]:
    """Return canonical IDs from *path*, treating unreadable source as empty.

    Static duration and invariant guards must fail closed when a test module
    cannot be read or parsed.  Keeping that policy here prevents individual
    callers from accidentally preserving stale duration entries or treating a
    syntactically-invalid module as executable.
    """
    try:
        return extract_python_test_ids(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return set()


def extract_python_test_nodes(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Select canonical IDs and the AST node that implements each test.

    Consumers that inspect a test body must select from this collector-faithful
    mapping first.  In particular, a ``test_*`` method on an arbitrary helper
    class is not a test, and inherited methods retain the subclass-qualified
    identity reported by pytest/unittest.
    """
    top_level_classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    classes_by_name = {node.name: node for node in top_level_classes}
    unittest_classes = _unittest_subclass_names(top_level_classes)

    test_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                test_nodes[node.name] = node
            continue
        if not isinstance(node, ast.ClassDef) or not (
            _has_pytest_class_name(node.name) or node.name in unittest_classes
        ):
            continue
        test_nodes.update(
            {
                f"{node.name}::{method_name}": method
                for method_name, method in _inherited_test_methods(
                    node, classes_by_name
                ).items()
            }
        )
    return test_nodes


def _extract_python_test_ids_from_tree(tree: ast.Module) -> set[str]:
    """Extract canonical identities from one already-parsed Python module."""
    return set(extract_python_test_nodes(tree))


def count_python_test_ids(source: str) -> int:
    """Return the collector-faithful number of Python test identities.

    Keep count-based guards on the same identity universe as duration and
    protection consumers instead of allowing each caller to invent a second
    counting rule.
    """
    return len(extract_python_test_ids(source))


_TS_DIRECT_TEST_RE = re.compile(
    r'''^\s*(?:it|test)\s*\(\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)''',
    re.MULTILINE,
)
_TS_PARAMETERIZED_TEST_RE = re.compile(
    r'''^\s*(?:it|test)\.each\b[\s\S]*?\)\s*\(\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)''',
    re.MULTILINE,
)


def extract_typescript_test_ids(source: str) -> set[str]:
    """Return labels from direct and ``.each`` Vitest/Jest declarations.

    This is intentionally the single Python-side TypeScript identity extractor:
    manifest protection and viewport auditing must count the same backtick,
    quoted, and parameterized declarations.
    """
    return {
        label
        for pattern in (_TS_DIRECT_TEST_RE, _TS_PARAMETERIZED_TEST_RE)
        for match in pattern.finditer(source)
        if (label := next((group for group in match.groups() if group), None))
    }


def _has_pytest_class_name(name: str) -> bool:
    """Match Blueprint's configured pytest ``python_classes`` conventions."""
    return name.startswith("Test") or name.endswith("Test")


def _inherited_test_methods(
    node: ast.ClassDef, classes_by_name: dict[str, ast.ClassDef]
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return ``test_*`` methods on a class plus module-local inherited ones.

    An override and its base definition collapse to one identity because the
    collector reports one node ID per name on the subclass.
    """
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    visited: set[str] = set()
    pending = [node]
    while pending:
        current = pending.pop()
        if current.name in visited:
            continue
        visited.add(current.name)
        methods.update(
            {
                member.name: member
                for member in current.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name.startswith("test_")
                and member.name not in methods
            }
        )
        pending.extend(
            classes_by_name[base.id]
            for base in current.bases
            if isinstance(base, ast.Name) and base.id in classes_by_name
        )
    return methods


def _unittest_subclass_names(classes: list[ast.ClassDef]) -> set[str]:
    """Resolve direct and module-local indirect ``unittest.TestCase`` bases."""
    subclasses = {
        node.name
        for node in classes
        if any(_is_direct_test_case_base(base) for base in node.bases)
    }
    while True:
        discovered = subclasses | {
            node.name
            for node in classes
            if any(
                isinstance(base, ast.Name) and base.id in subclasses
                for base in node.bases
            )
        }
        if discovered == subclasses:
            return subclasses
        subclasses = discovered


def _is_direct_test_case_base(base: ast.expr) -> bool:
    """Return whether an AST base directly names ``unittest.TestCase``."""
    return (isinstance(base, ast.Name) and base.id == "TestCase") or (
        isinstance(base, ast.Attribute) and base.attr == "TestCase"
    )
