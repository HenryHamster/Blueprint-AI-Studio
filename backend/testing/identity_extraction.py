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
    tree = ast.parse(source)
    top_level_classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    classes_by_name = {node.name: node for node in top_level_classes}
    unittest_classes = _unittest_subclass_names(top_level_classes)

    test_ids: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                test_ids.add(node.name)
            continue
        if not isinstance(node, ast.ClassDef) or not (
            _has_pytest_class_name(node.name) or node.name in unittest_classes
        ):
            continue
        test_ids.update(
            f"{node.name}::{method_name}"
            for method_name in _inherited_test_method_names(node, classes_by_name)
        )
    return test_ids


def _has_pytest_class_name(name: str) -> bool:
    """Match Blueprint's configured pytest ``python_classes`` conventions."""
    return name.startswith("Test") or name.endswith("Test")


def _inherited_test_method_names(
    node: ast.ClassDef, classes_by_name: dict[str, ast.ClassDef]
) -> set[str]:
    """Return ``test_*`` methods on a class plus those from module-local bases.

    An override and its base definition collapse to one identity because the
    collector reports one node ID per name on the subclass.
    """
    method_names: set[str] = set()
    visited: set[str] = set()
    pending = [node]
    while pending:
        current = pending.pop()
        if current.name in visited:
            continue
        visited.add(current.name)
        method_names.update(
            member.name
            for member in current.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test_")
        )
        pending.extend(
            classes_by_name[base.id]
            for base in current.bases
            if isinstance(base, ast.Name) and base.id in classes_by_name
        )
    return method_names


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
