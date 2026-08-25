"""Decide which backend test modules a timed run can actually measure.

WHY THIS EXISTS
---------------
``test_test_durations_freshness.py`` asserts that every module in the backend
test roster carries at least one entry in ``backend/.test_durations``. That is
the right assertion for a module pytest runs, and an impossible one for a module
pytest never runs: ``--store-durations`` records one duration per *executed*
test, so a module that contributes no executed test contributes no entry -- in
every environment, on every host, forever.

Two modules in the roster are in exactly that position today, and neither is a
mistake:

* ``testing/test_agent_task_contract.py`` holds shared contract assertions
  imported by other suites. It declares itself with the repository's existing
  ``# blueprint-test-module: intentional-zero-tests -- <reason>`` marker, whose
  required shape ``test_invariants.py`` already enforces.
* ``testing/test_lifecycle_integration.py`` is ``pytest.mark.integration``, and
  ``backend/pyproject.toml`` sets ``addopts = "-m 'not integration' ..."`` --
  the suite deselects it by default, so no ordinary run reaches it.

Demanding coverage from either is demanding evidence that cannot exist. Left
alone that is not merely noise: an unsatisfiable clause in a promotion gate is
permanent debt which every future candidate inherits and no candidate can
discharge, and a gate that always reports the same two names trains its readers
to stop reading it.

WHY THIS IS NOT A HOLE IN THE GUARD
-----------------------------------
Every exclusion here is DERIVED, never listed. No filename appears anywhere in
this module. A module is excluded only on positive evidence found in its own
source, and only for a reason the repository states independently somewhere
else:

* Delete the marker comment from the helper module and it is demanded again.
* Remove ``not integration`` from ``addopts`` -- that is, make the suite
  actually run those tests -- and every integration module is demanded again.

The exclusion set therefore cannot be widened by editing this file. It can only
be widened by making some module genuinely unmeasurable, which is reviewable at
the place it happens rather than here.

Nothing is excused on an error. A missing or unparseable ``pyproject.toml``, an
``addopts`` with no ``-m`` selector, or a module whose source cannot be read or
parsed all yield NO exclusion: the guard goes on demanding coverage. The
failure direction is deliberate -- an unreadable input must never widen a gate's
blind spot, which is the vacuous-guard shape this repository has paid for
repeatedly.
"""

from __future__ import annotations

import ast
import re
import shlex
import tomllib
from pathlib import Path

__all__ = [
    "ZERO_TEST_MARKER",
    "deselected_markers",
    "module_pytestmarks",
    "unmeasurable_reason",
]

# The repository's existing declaration for a ``test_*.py`` module that holds
# helpers rather than tests. ``test_invariants.py`` pins the required shape (the
# marker must carry a ``-- <reason>`` justification); this only detects it.
ZERO_TEST_MARKER = "# blueprint-test-module: intentional-zero-tests"

# A bare ``not <marker>`` term inside a ``-m`` selector expression. Anything more
# involved than that -- ``not (a or b)``, a parenthesised group -- matches
# nothing here on purpose, leaving the module demanded rather than guessed at.
_NOT_TERM = re.compile(r"\bnot\s+(?P<marker>[A-Za-z_][A-Za-z0-9_]*)\b")


def deselected_markers(pyproject_path: Path | str) -> frozenset[str]:
    """Return the markers the suite's default ``addopts`` deselects.

    Read out of ``[tool.pytest.ini_options] addopts`` rather than restated here,
    so this cannot disagree with what pytest actually does.
    """
    try:
        raw = Path(pyproject_path).read_bytes()
        config = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return frozenset()

    addopts = (
        config.get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("addopts")
    )
    # pytest accepts addopts as a string it splits itself, or as an already
    # split list. Joining the list and re-splitting would destroy the token
    # boundaries the author established -- ``['-m', 'not integration']`` would
    # come back as three tokens and the selector would read as ``-m not``.
    if isinstance(addopts, list):
        tokens = [str(part) for part in addopts]
    elif isinstance(addopts, str) and addopts:
        try:
            tokens = shlex.split(addopts)
        except ValueError:
            return frozenset()
    else:
        return frozenset()

    markers: set[str] = set()
    for index, token in enumerate(tokens):
        expression = None
        if token == "-m" and index + 1 < len(tokens):
            expression = tokens[index + 1]
        elif token.startswith("-m") and len(token) > 2:
            expression = token[2:]
        if expression:
            markers.update(match.group("marker") for match in _NOT_TERM.finditer(expression))
    return frozenset(markers)


def module_pytestmarks(tree: ast.Module) -> frozenset[str]:
    """Return marker names applied to a whole module via ``pytestmark``.

    Handles the single-marker and list-of-markers spellings, which are the two
    pytest documents. A dynamically computed ``pytestmark`` yields nothing --
    the module stays demanded rather than excused on a guess.
    """
    names: set[str] = set()

    def _marker_name(node: ast.expr) -> str | None:
        # ``pytest.mark.integration`` and ``pytest.mark.integration(...)``.
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
            if node.value.attr == "mark":
                return node.attr
        return None

    for statement in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            targets, value = list(statement.targets), statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        if value is None:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for element in elements:
            name = _marker_name(element)
            if name is not None:
                names.add(name)
    return frozenset(names)


def unmeasurable_reason(
    path: Path | str, deselected: frozenset[str]
) -> str | None:
    """Return why a timed run cannot measure ``path``, or None if it can.

    The string is the human-facing justification, so a reader of the guard's
    output sees the reason rather than a bare exemption.
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # Unreadable: keep demanding coverage.

    if ZERO_TEST_MARKER in source:
        return "declares itself an intentional-zero-tests helper module"

    if not deselected:
        return None

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None  # Unparseable: keep demanding coverage.

    applied = module_pytestmarks(tree) & deselected
    if applied:
        marker = sorted(applied)[0]
        return f"is module-level pytest.mark.{marker}, which addopts deselects"
    return None
