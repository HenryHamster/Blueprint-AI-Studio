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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _TypeScriptCall:
    """One statically recognizable Vitest declaration."""

    kind: str
    start: int
    label: str | None
    row_count: int
    body_start: int | None = None
    body_end: int | None = None


def extract_typescript_test_ids(
    source: str, path: Path | None = None
) -> set[str]:
    """Return collector-shaped identities from Vitest/Jest test source.

    Vitest's real identity is the describe ancestry plus the test title.  A
    bare-label set loses same-named tests in different suites and treats an
    ``it.each`` table as one test.  This bounded scanner deliberately parses
    only declaration syntax: it skips comments and strings, balances nested
    delimiters, preserves escaped label delimiters, qualifies nested
    ``describe`` scopes, and expands statically representable array rows.

    IDs use Vitest's human-readable ``describe > test`` shape. Parameter rows
    receive a stable ``[case N]`` suffix because the source title may be the
    same for every row and the manifest needs one identity per collected test.
    """
    row_counts = _typescript_static_array_row_counts(source, path)
    calls = _scan_typescript_calls(source, row_counts)
    describes = [call for call in calls if call.kind == "describe"]
    test_ids: set[str] = set()
    for call in calls:
        if call.kind != "test" or call.label is None:
            continue
        ancestors = [
            describe
            for describe in describes
            if describe.body_start is not None
            and describe.body_end is not None
            and describe.body_start <= call.start < describe.body_end
        ]
        ancestors.sort(key=lambda describe: describe.start)
        prefix = [describe.label for describe in ancestors if describe.label]
        title = " > ".join([*prefix, call.label])
        for row in range(1, call.row_count + 1):
            candidate = title if call.row_count == 1 else f"{title} [case {row}]"
            if candidate in test_ids:
                duplicate = 2
                while f"{candidate} [duplicate {duplicate}]" in test_ids:
                    duplicate += 1
                candidate = f"{candidate} [duplicate {duplicate}]"
            test_ids.add(candidate)
    return test_ids


def _scan_typescript_calls(
    source: str, row_counts: dict[str, int] | None = None
) -> list[_TypeScriptCall]:
    """Scan declaration calls without treating comments or strings as code."""
    calls: list[_TypeScriptCall] = []
    index = 0
    while index < len(source):
        index = _skip_typescript_trivia(source, index)
        if index >= len(source):
            break
        if source[index] in "'\"`":
            index = _typescript_string_end(source, index)
            continue
        if source[index] == "/" and _looks_like_typescript_regex(source, index):
            index = _typescript_regex_end(source, index)
            continue
        if not (source[index].isalpha() or source[index] in "_$"):
            index += 1
            continue

        identifier_start = index
        index += 1
        while index < len(source) and (source[index].isalnum() or source[index] in "_$"):
            index += 1
        identifier = source[identifier_start:index]
        if identifier not in {"describe", "it", "test"}:
            continue
        if identifier_start > 0 and source[identifier_start - 1] in ".$_":
            continue

        cursor = _skip_typescript_trivia(source, index)
        is_each = False
        if source.startswith(".each", cursor):
            after_each = cursor + len(".each")
            if after_each == len(source) or not (source[after_each].isalnum() or source[after_each] in "_$"):
                is_each = True
                cursor = _skip_typescript_trivia(source, after_each)
        while True:
            modifier = next(
                (
                    name
                    for name in ("skip", "only", "fails", "concurrent")
                    if source.startswith(f".{name}", cursor)
                    and (
                        cursor + len(name) + 1 == len(source)
                        or not (source[cursor + len(name) + 1].isalnum() or source[cursor + len(name) + 1] in "_$")
                    )
                ),
                None,
            )
            if modifier is None:
                break
            cursor = _skip_typescript_trivia(source, cursor + len(modifier) + 1)
        if cursor >= len(source) or source[cursor] != "(":
            continue

        data_end = _matching_typescript_delimiter(source, cursor)
        if data_end is None:
            continue
        label_start = _skip_typescript_trivia(source, data_end + 1) if is_each else cursor + 1
        label_end = label_start
        if is_each:
            if label_start >= len(source) or source[label_start] != "(":
                continue
            label_end = _matching_typescript_delimiter(source, label_start) or label_start
            label = _typescript_call_label(source, label_start + 1)
            row_count = _typescript_each_row_count(
                source[cursor + 1 : data_end], row_counts
            )
        else:
            label = _typescript_call_label(source, label_start)
            label_end = data_end
            row_count = 1
        if label is None:
            continue

        body_start = body_end = None
        if identifier == "describe":
            callback_start = _find_callback_body(source, label_start, label_end)
            if callback_start is not None:
                body_start = callback_start + 1
                body_end = _matching_typescript_delimiter(source, callback_start)
        calls.append(
            _TypeScriptCall(
                kind="describe" if identifier == "describe" else "test",
                start=identifier_start,
                label=label,
                row_count=row_count,
                body_start=body_start,
                body_end=body_end,
            )
        )
        # Continue through a describe callback so nested declarations are
        # discovered; the lexical scanner already skips the declaration's
        # label and callback strings as it walks.
        index = identifier_start + len(identifier)
    return calls


def _skip_typescript_trivia(source: str, index: int) -> int:
    """Skip whitespace and JS comments while scanning declaration syntax."""
    while index < len(source):
        if source[index].isspace():
            index += 1
        elif source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline == -1 else newline + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end == -1 else end + 2
        else:
            break
    return index


def _matching_typescript_delimiter(source: str, opening: int) -> int | None:
    """Find a matching bracket while ignoring strings and comments."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = pairs.get(source[opening])
    if closing is None:
        return None
    stack = [closing]
    index = opening + 1
    while index < len(source):
        char = source[index]
        if char in "'\"`":
            index = _typescript_string_end(source, index)
            continue
        if char == "/" and _looks_like_typescript_regex(source, index):
            index = _typescript_regex_end(source, index)
            continue
        if source.startswith("//", index):
            index = _skip_typescript_trivia(source, index)
            continue
        if source.startswith("/*", index):
            index = _skip_typescript_trivia(source, index)
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char == stack[-1]:
            stack.pop()
            if not stack:
                return index
        index += 1
    return None


def _typescript_string_end(source: str, opening: int) -> int:
    """Return the first position after a JS string, honoring escaped quotes."""
    delimiter = source[opening]
    index = opening + 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
        elif source[index] == delimiter:
            return index + 1
        else:
            index += 1
    return len(source)


def _looks_like_typescript_regex(source: str, index: int) -> bool:
    """Recognize regex literals well enough to keep the declaration scanner aligned."""
    if index + 1 >= len(source) or source[index + 1] in "/*":
        return False
    previous = index - 1
    while previous >= 0 and source[previous].isspace():
        previous -= 1
    return previous < 0 or source[previous] in "([{=,:;!?&|"


def _typescript_regex_end(source: str, opening: int) -> int:
    """Return the first position after a regex literal and its flags."""
    index = opening + 1
    in_class = False
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(source) and (source[index].isalpha() or source[index] in "_$"):
                index += 1
            return index
        index += 1
    return len(source)


def _typescript_call_label(source: str, argument_start: int) -> str | None:
    """Read a static first string argument and decode ordinary JS escapes."""
    index = _skip_typescript_trivia(source, argument_start)
    if index >= len(source) or source[index] not in "'\"`":
        return None
    end = _typescript_string_end(source, index)
    raw = source[index + 1 : max(index + 1, end - 1)]
    try:
        return ast.literal_eval(source[index:end]) if source[index] != "`" else _decode_typescript_template(raw)
    except (SyntaxError, ValueError):
        return raw.replace("\\" + source[index], source[index]).replace("\\\\", "\\")


def _decode_typescript_template(raw: str) -> str:
    """Decode only template escapes; interpolation remains a stable source label."""
    return raw.replace("\\`", "`").replace("\\\\", "\\").replace("\\n", "\n")


def _find_callback_body(source: str, label_start: int, call_end: int) -> int | None:
    """Locate the callback block following a describe label."""
    index = _skip_typescript_trivia(source, label_start)
    if index < len(source) and source[index] in "'\"`":
        index = _typescript_string_end(source, index)
    while index < len(source) and index < call_end:
        if source[index] == "{":
            return index
        if source[index] in "'\"`":
            index = _typescript_string_end(source, index)
        else:
            index += 1
    return None


def _typescript_each_row_count(
    data: str, row_counts: dict[str, int] | None = None
) -> int:
    """Count top-level rows in a static array passed to ``.each``."""
    start = _skip_typescript_trivia(data, 0)
    if start >= len(data) or data[start] != "[":
        reference = data[start:].strip()
        if row_counts is not None and re.fullmatch(r"[A-Za-z_$][\w$]*", reference):
            return row_counts.get(reference, 1)
        return 1
    end = _matching_typescript_delimiter(data, start)
    if end is None:
        return 1
    rows = 0
    item_start = start + 1
    index = item_start
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    while index < end:
        char = data[index]
        if char in "'\"`":
            index = _typescript_string_end(data, index)
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == "," and not stack:
            if data[item_start:index].strip():
                rows += 1
            item_start = index + 1
        index += 1
    if data[item_start:end].strip():
        rows += 1
    return rows


def _typescript_static_array_row_counts(
    source: str,
    path: Path | None,
    seen: set[Path] | None = None,
) -> dict[str, int]:
    """Resolve local and relative-imported static arrays used by ``*.each``.

    This intentionally handles only literal arrays and aliases. Dynamic
    expressions remain conservative at one row, while a supplied source path
    lets inventory callers follow a relative import such as
    ``ALL_AGENT_TASK_TYPES`` without executing TypeScript.
    """
    seen = set() if seen is None else seen
    normalized_path = path.resolve() if path is not None else None
    if normalized_path is not None:
        if normalized_path in seen:
            return {}
        seen.add(normalized_path)

    bindings: dict[str, int | str] = {}
    declaration_re = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
        r"(?:\s*:\s*[\s\S]*?)?\s*=",
        re.DOTALL,
    )
    for match in declaration_re.finditer(source):
        name = match.group(1)
        value_start = _skip_typescript_trivia(source, match.end())
        if value_start < len(source) and source[value_start] == "[":
            value_end = _matching_typescript_delimiter(source, value_start)
            if value_end is not None:
                bindings[name] = _typescript_each_row_count(
                    source[value_start : value_end + 1]
                )
        else:
            alias = re.match(r"([A-Za-z_$][\w$]*)", source[value_start:])
            if alias:
                bindings[name] = alias.group(1)

    imported_bindings: dict[str, tuple[Path, str]] = {}
    import_re = re.compile(
        r"\bimport\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]"
    )
    for match in import_re.finditer(source):
        if path is None or not match.group(2).startswith("."):
            continue
        module_path = _resolve_typescript_module(path.parent, match.group(2))
        if module_path is None:
            continue
        for specifier in match.group(1).split(","):
            parts = re.split(r"\s+as\s+", specifier.strip())
            imported = parts[0].strip()
            local = parts[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", imported) and re.fullmatch(
                r"[A-Za-z_$][\w$]*", local
            ):
                imported_bindings[local] = (module_path, imported)

    resolved: dict[str, int] = {}
    pending = dict(bindings)
    for local, (module_path, imported) in imported_bindings.items():
        try:
            imported_source = module_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        imported_counts = _typescript_static_array_row_counts(
            imported_source, module_path, seen
        )
        if imported in imported_counts:
            pending[local] = imported_counts[imported]

    while pending:
        progress = False
        for name, value in list(pending.items()):
            if isinstance(value, int):
                resolved[name] = value
                del pending[name]
                progress = True
            elif value in resolved:
                resolved[name] = resolved[value]
                del pending[name]
                progress = True
        if not progress:
            break
    return resolved


def _resolve_typescript_module(directory: Path, specifier: str) -> Path | None:
    """Resolve a relative TypeScript import without leaving the workspace tree."""
    candidate = (directory / specifier).resolve()
    candidates = [candidate]
    if candidate.suffix == ".js" or not candidate.suffix:
        candidates.extend(candidate.with_suffix(ext) for ext in (".ts", ".tsx", ".js"))
    candidates.extend(candidate / f"index{ext}" for ext in (".ts", ".tsx", ".js"))
    return next((item for item in candidates if item.is_file()), None)


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
