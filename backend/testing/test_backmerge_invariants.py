"""Static-guard tests for the back-merge and promotion modules.

These tests assert that hotfix_backmerge.py and release_promotion.py never
embed literal branch-name pairs ('main -> automain') or the bare constant
'automain' in Task/Alert constructor field values, and that spawn payloads do
not hardcode base_branch='automain'.

Guarded regression: before the per-project integration_branch column landed,
the conflict-resolution Task was titled
'Resolve back-merge conflict: main -> automain' regardless of the project's
actual branch names.  The tests here would have caught that.

Run standalone: pytest testing/test_backmerge_invariants.py -v --noconftest
"""

import ast
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Source-text helpers
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
_BACKMERGE = _APP_DIR / "hotfix_backmerge.py"
_PROMOTION = _APP_DIR / "release_promotion.py"

# Matches literal branch-name pairs, with or without Unicode arrow.
_PAIR_RE = re.compile(
    r"(?:main\s*[-→>]+\s*automain|automain\s*[-→>]+\s*main)",
    re.UNICODE,
)

# Matches 'automain' or "automain" as a standalone quoted token.
_BARE_AUTOMAIN_RE = re.compile(r"""(?:"|')automain(?:"|')""")

# Matches {"base_branch": "automain"} dict literals; exempts .get("base_branch",
# "automain") — where a '(' or ',' plus space precedes the key — via lookbehind.
_BASE_BRANCH_AUTOMAIN_RE = re.compile(r'(?<![(,]\s)"base_branch":\s*"automain"')


def _docstring_lines(src: str) -> set[int]:
    """Return 1-based line numbers that belong to module/function/class docstrings.

    Uses the AST so multi-line triple-quoted strings are handled exactly.
    """
    exempt: set[int] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return exempt
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(getattr(first, "value", None), ast.Constant)
            and isinstance(first.value.value, str)
        ):
            start = first.lineno
            end = getattr(first, "end_lineno", first.lineno) or first.lineno
            for lineno in range(start, end + 1):
                exempt.add(lineno)
    return exempt


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, text) pairs for lines outside docstrings and not comments."""
    src = path.read_text(encoding="utf-8")
    exempt = _docstring_lines(src)
    result = []
    for i, line in enumerate(src.splitlines(), start=1):
        if i in exempt:
            continue
        if line.lstrip().startswith("#"):
            continue
        result.append((i, line))
    return result


def _string_parts(node: ast.expr):
    """Yield str-typed leaf constants reachable from an AST expression.

    Covers ast.Constant (plain str), ast.JoinedStr (f-string), and
    ast.BinOp(Add) (implicit string concatenation).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            yield from _string_parts(part)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield from _string_parts(node.left)
        yield from _string_parts(node.right)


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


class TestBackmergeHardcodedBranches:
    """Static guards against hardcoded branch-name constants in the back-merge
    and promotion modules.

    Each test reads the source of the target module(s) and asserts the absence
    of a specific anti-pattern.  Tests are deliberately narrow so they fire on
    the exact regression class without flagging correct dynamic patterns.
    """

    def test_no_hardcoded_main_automain_pair_in_task_or_alert_fields(self):
        """Task/Alert constructor kwargs title=, description=, and message= must
        not contain the literal pair 'main -> automain' or 'automain -> main'
        (including the '→' Unicode variant).

        Correct code uses the dynamic variables returned by _lts_branch_names():
            title=f"Resolve back-merge conflict: {promotion_target} -> {integration_branch}"
        A literal pair means the message is wrong for any project whose branch
        names differ from the Blueprint self-repo defaults.

        The check uses AST walking so f-string interpolations ({var}) inside the
        value are inspected properly: only literal string parts of the kwarg
        value trigger the assertion; fully-interpolated tokens do not.
        """
        offenders = []
        for path in (_BACKMERGE, _PROMOTION):
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg not in ("title", "description", "message"):
                        continue
                    for part in _string_parts(kw.value):
                        if _PAIR_RE.search(part):
                            lineno = getattr(kw.value, "lineno", "?")
                            offenders.append(
                                f"{path.name}:{lineno}: {kw.arg}= contains "
                                f"hardcoded branch pair in literal: {part!r:.120}"
                            )

        assert not offenders, (
            "Task/Alert constructor kwarg (title / description / message) "
            "contains a hardcoded branch-name pair ('main -> automain' or "
            "'automain -> main').\n"
            "Use dynamic variables from _lts_branch_names() instead:\n"
            + "\n".join(offenders)
        )

    def test_no_bare_automain_string_literal_outside_docstrings(self):
        """No non-docstring, non-comment code line in either module may contain
        the bare quoted literal 'automain' or \"automain\" as a standalone token.

        Correct code obtains the integration-branch name from _lts_branch_names()
        and stores it in a local variable (integration_branch / promotion_target).
        A bare 'automain' constant in code hardcodes a Blueprint self-repo
        assumption that breaks for any project with different branch names.

        Docstring references (e.g. '``automain``') are excluded by the AST-based
        line filter and do not trigger this assertion.
        """
        offenders = []
        for path in (_BACKMERGE, _PROMOTION):
            for lineno, line in _code_lines(path):
                if _BARE_AUTOMAIN_RE.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")

        assert not offenders, (
            "Bare string literal 'automain' found in non-docstring, non-comment "
            "code.  Use integration_branch / promotion_target from "
            "_lts_branch_names() instead:\n" + "\n".join(offenders)
        )

    def test_no_hardcoded_automain_in_base_branch_payload(self):
        """Spawn payloads in hotfix_backmerge.py must never hardcode
        {"base_branch": "automain"} — they must use the integration_branch
        variable from _lts_branch_names() so the per-project column takes effect.

        Inheritance reads like .get("base_branch", "automain") (where '(' or ','
        plus a space precede the key) are a different, correct pattern and are
        allowed via the lookbehind in _BASE_BRANCH_AUTOMAIN_RE.

        Mirrors the existing guard in test_task_lifecycle.py for 'main'.
        """
        offenders = [
            f"hotfix_backmerge.py:{lineno}: {line.strip()}"
            for lineno, line in _code_lines(_BACKMERGE)
            if _BASE_BRANCH_AUTOMAIN_RE.search(line)
        ]
        assert not offenders, (
            "hotfix_backmerge.py spawn payload hardcodes base_branch='automain' "
            "instead of routing through integration_branch from "
            "_lts_branch_names():\n" + "\n".join(offenders)
        )
