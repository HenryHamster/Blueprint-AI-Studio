"""Standalone guard: no test in backend/testing imports a symbol that moved.

The gate here is :func:`test_backend_testing_corpus_has_no_stale_app_imports`,
which resolves every ``from app.…`` / ``import app.…`` statement in the
backend test corpus against the app tree on disk.  The rest of the module is
the mutation proof for that gate: synthetic trees that assert the checker fires
on each defect shape and stays silent on each legitimate one.

Why the gate exists
-------------------
PR #17578 split ``backend/app/release_qualifier_sweep.py`` and moved
``_AUTOMAIN_HEAD_FIRST_SEEN_KEY`` into ``backend/app/release_qualifier_debounce.py``.
It missed three imports in
``backend/testing/test_release_qualifier__trigger_and_dispatch_regressions.py``.
The resulting ``ImportError`` stayed red on ``automain`` for four days and only
surfaced at the ``automain`` -> ``main`` promotion PR, where it failed four
tests across three CI shards and blocked a release.

Neither of the two reasons it was invisible is going away:

* ``automain`` PRs run **zero** CI checks by design (``ci.yml``'s
  ``pull_request`` trigger is ``branches: [main]``), so the deterministic gate
  is the promotion PR -- days after the merge that broke it.
* All three imports were **function-local**, so ``pytest --collect-only``
  imports the module cleanly and reports no error.  A collection-integrity
  check cannot see this class at all.

A static walk is the only cheap thing that can, which is why the checker parses
rather than imports (the standalone lane has no Postgres, and every app module
drags in SQLAlchemy and a database engine) and why this file is on the
standalone roster rather than an ordinary DB-backed shard.

Runs under --noconftest (no DB, no conftest fixtures).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from testing import check_test_imports as cti
from testing.fixgate_attribution import format_offender_marker

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = Path(cti.__file__).resolve()


def _write(path: Path, text: str) -> None:
    """Write LF-only bytes.

    ``Path.write_text`` emits CRLF on a Windows checkout, which shifts nothing
    semantically but does change what a byte-oriented fixture asserts. Writing
    bytes keeps these trees identical on every host the lane runs on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))


def _tree(
    tmp_path: Path,
    app_modules: dict[str, str],
    test_files: dict[str, str],
) -> Path:
    """Build a miniature repo: ``backend/app`` + ``backend/testing``."""
    root = tmp_path / "repo"
    app_dir = root / "backend" / "app"
    testing_dir = root / "backend" / "testing"
    app_dir.mkdir(parents=True)
    testing_dir.mkdir(parents=True)
    _write(app_dir / "__init__.py", "")
    for rel, source in app_modules.items():
        _write(app_dir / rel, source)
    for rel, source in test_files.items():
        _write(testing_dir / rel, source)
    return root


def _run(root: Path) -> list[cti.Violation]:
    return cti.find_stale_test_imports(root=root)


def _identity(violation: cti.Violation) -> str:
    """Single-line offender identity (the marker rejects embedded newlines)."""
    return (
        f"{violation.test_path}:{violation.lineno}:"
        f"{violation.kind}:{violation.statement}"
    )


# --------------------------------------------------------------------------
# The gate.
# --------------------------------------------------------------------------


def test_backend_testing_corpus_has_no_stale_app_imports() -> None:
    """Every ``app`` import in backend/testing resolves against the app tree.

    This is the whole point of the module: it runs on every path into
    ``automain``, so an import orphaned by a module split fails here at merge
    time instead of surfacing days later on the promotion PR (#17578).
    """
    violations = cti.find_stale_test_imports(root=REPO_ROOT)
    offenders = sorted(_identity(violation) for violation in violations)
    detail = "\n".join(violation.format() for violation in violations)
    assert not offenders, (
        "Test files import names that no longer exist where they are imported "
        "from. Each one raises ImportError the moment the test body runs, and "
        "-- if it is a function-local import -- pytest collection will not "
        "notice. Point the import at the module that owns the symbol now:\n"
        f"{detail}\n"
        + format_offender_marker(offenders)
    )


# --------------------------------------------------------------------------
# Mutation proof: the checker fires on each defect shape.
# --------------------------------------------------------------------------


def test_function_local_stale_import_is_caught(tmp_path: Path) -> None:
    """The exact #17578 shape: the constant moved, the test body did not.

    Reproduced literally -- module split, constant re-homed, a *function-local*
    import left naming the old module. If this ever stops failing the guard has
    regressed to what ``--collect-only`` could already do, which is nothing.
    """
    root = _tree(
        tmp_path,
        {
            "release_qualifier_debounce.py": (
                '_AUTOMAIN_HEAD_FIRST_SEEN_KEY = "_automain_head_first_seen"\n'
            ),
            "release_qualifier_sweep.py": (
                "from app.release_qualifier_debounce import (\n"
                "    compute_automain_head_quiet_seconds,\n"
                ")\n"
            ),
        },
        {
            "test_sweep.py": (
                "async def _age_automain_head_marker(session):\n"
                "    from app.release_qualifier_sweep import "
                "_AUTOMAIN_HEAD_FIRST_SEEN_KEY\n"
                "\n"
                "    return _AUTOMAIN_HEAD_FIRST_SEEN_KEY\n"
            )
        },
    )

    violations = _run(root)
    assert len(violations) == 1, "the function-local stale import was not caught"
    assert violations[0].kind == "stale-name"
    assert violations[0].lineno == 2
    assert "_AUTOMAIN_HEAD_FIRST_SEEN_KEY" in violations[0].statement
    assert "release_qualifier_sweep" in violations[0].detail


def test_stale_import_survives_a_re_export_being_deleted(tmp_path: Path) -> None:
    """Re-exporting the symbol clears the finding; deleting it brings it back.

    This is the real regression history: #17578 fixed the break by re-exporting
    the constant from the old module, and a later unused-import cleanup
    (#17663) deleted the re-export and silently reintroduced the ImportError.
    Both directions are pinned so neither repair nor relapse is invisible.
    """
    with_reexport = _tree(
        tmp_path / "with",
        {
            "release_qualifier_debounce.py": '_KEY = "k"\n',
            "release_qualifier_sweep.py": (
                "from app.release_qualifier_debounce import _KEY\n"
            ),
        },
        {
            "test_sweep.py": (
                "def test_marker():\n"
                "    from app.release_qualifier_sweep import _KEY\n"
                "\n"
                "    assert _KEY\n"
            )
        },
    )
    assert _run(with_reexport) == [], (
        "a genuine re-export must not be reported as stale"
    )

    without_reexport = _tree(
        tmp_path / "without",
        {
            "release_qualifier_debounce.py": '_KEY = "k"\n',
            "release_qualifier_sweep.py": "OTHER = 1\n",
        },
        {
            "test_sweep.py": (
                "def test_marker():\n"
                "    from app.release_qualifier_sweep import _KEY\n"
                "\n"
                "    assert _KEY\n"
            )
        },
    )
    relapsed = _run(without_reexport)
    assert [violation.kind for violation in relapsed] == ["stale-name"], (
        "deleting the re-export must reinstate the finding"
    )


def test_imports_are_checked_at_every_nesting_depth(tmp_path: Path) -> None:
    """Class bodies, ``if`` blocks and nested functions are all walked.

    A module-body-only scan is the failure mode this guards against: three of
    the four sites below are invisible to one.
    """
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {
            "test_nested.py": (
                "from app.target import GONE_MODULE_LEVEL\n"
                "\n"
                "\n"
                "class TestThing:\n"
                "    from app.target import GONE_CLASS_BODY\n"
                "\n"
                "    def test_inner(self):\n"
                "        if True:\n"
                "            from app.target import GONE_IF_BLOCK\n"
                "\n"
                "        def helper():\n"
                "            from app.target import GONE_NESTED_FUNCTION\n"
                "\n"
                "        return helper\n"
            )
        },
    )

    violations = _run(root)
    reported = {violation.statement for violation in violations}
    assert reported == {
        "from app.target import GONE_MODULE_LEVEL",
        "from app.target import GONE_CLASS_BODY",
        "from app.target import GONE_IF_BLOCK",
        "from app.target import GONE_NESTED_FUNCTION",
    }, "the walk missed at least one nesting depth"


def test_nonexistent_module_is_caught(tmp_path: Path) -> None:
    """Both import forms fail when ``backend/app/<mod>.py`` is simply absent."""
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {
            "test_missing.py": (
                "from app.no_such_module import thing\n"
                "import app.also_missing\n"
                "\n"
                "\n"
                "def test_local():\n"
                "    import app.missing_too as alias\n"
                "\n"
                "    return alias\n"
            )
        },
    )

    violations = _run(root)
    assert [violation.kind for violation in violations] == [
        "unknown-module",
        "unknown-module",
        "unknown-module",
    ], "a nonexistent app module was not reported"
    statements = {violation.statement for violation in violations}
    assert statements == {
        "from app.no_such_module import thing",
        "import app.also_missing",
        "import app.missing_too",
    }


def test_a_function_local_name_is_not_a_module_export(tmp_path: Path) -> None:
    """A name bound inside the target module's own functions is not importable.

    Walking the target with a plain ``ast.walk`` would hoover up locals and
    quietly pass this stale import; the module-scope walk must not.
    """
    root = _tree(
        tmp_path,
        {
            "target.py": (
                "def builder():\n"
                "    LOCAL_ONLY = 1\n"
                "    return LOCAL_ONLY\n"
                "\n"
                "\n"
                "class Holder:\n"
                "    ATTRIBUTE_ONLY = 2\n"
            )
        },
        {
            "test_locals.py": (
                "from app.target import LOCAL_ONLY\n"
                "from app.target import ATTRIBUTE_ONLY\n"
            )
        },
    )

    violations = _run(root)
    assert {violation.statement for violation in violations} == {
        "from app.target import LOCAL_ONLY",
        "from app.target import ATTRIBUTE_ONLY",
    }, "function/class-local names were mistaken for module exports"


# --------------------------------------------------------------------------
# False-positive proof: a bad report here wedges every merge, so each
# legitimate shape gets its own pin.
# --------------------------------------------------------------------------


def test_every_top_level_binding_form_counts_as_an_export(tmp_path: Path) -> None:
    """def / async def / class / assign / annotated / unpack / re-export / alias."""
    root = _tree(
        tmp_path,
        {
            "other.py": "SOURCE = 1\nRENAMED = 2\n",
            "target.py": (
                "from app.other import SOURCE\n"
                "from app.other import RENAMED as ALIASED\n"
                "\n"
                "CONSTANT = 1\n"
                "ANNOTATED: int = 2\n"
                "FIRST, SECOND = (3, 4)\n"
                "HEAD, *TAIL = [5, 6, 7]\n"
                "AUGMENTED = 0\n"
                "AUGMENTED += 1\n"
                "\n"
                "\n"
                "def plain_function():\n"
                "    return None\n"
                "\n"
                "\n"
                "async def async_function():\n"
                "    return None\n"
                "\n"
                "\n"
                "class SomeClass:\n"
                "    pass\n"
            ),
        },
        {
            "test_exports.py": (
                "from app.target import (\n"
                "    ALIASED,\n"
                "    ANNOTATED,\n"
                "    AUGMENTED,\n"
                "    CONSTANT,\n"
                "    FIRST,\n"
                "    HEAD,\n"
                "    SECOND,\n"
                "    SOURCE,\n"
                "    SomeClass,\n"
                "    TAIL,\n"
                "    async_function,\n"
                "    plain_function,\n"
                ")\n"
            )
        },
    )

    assert _run(root) == [], "a legitimate top-level binding form was flagged"


def test_conditional_module_level_bindings_count_as_exports(tmp_path: Path) -> None:
    """Names bound under ``if``/``try``/``with``/``for`` at module scope are real."""
    root = _tree(
        tmp_path,
        {
            "target.py": (
                "import contextlib\n"
                "from typing import TYPE_CHECKING\n"
                "\n"
                "if TYPE_CHECKING:\n"
                "    TYPE_ONLY = 1\n"
                "else:\n"
                "    TYPE_ONLY = 2\n"
                "\n"
                "try:\n"
                "    import tomllib\n"
                "except ModuleNotFoundError:\n"
                "    tomllib = None\n"
                "\n"
                "with contextlib.suppress(Exception):\n"
                "    MANAGED = 1\n"
                "\n"
                "for LOOPED in range(3):\n"
                "    pass\n"
            )
        },
        {
            "test_conditional.py": (
                "from app.target import LOOPED, MANAGED, TYPE_ONLY, tomllib\n"
            )
        },
    )

    assert _run(root) == [], "a conditionally bound module attribute was flagged"


def test_package_submodule_imports_resolve(tmp_path: Path) -> None:
    """``from app import x`` / ``from app.pkg import y`` name submodules."""
    root = _tree(
        tmp_path,
        {
            "shared_state.py": "runtime_config = {}\n",
            "routers/__init__.py": "",
            "routers/admin_health.py": "router = None\n",
        },
        {
            "test_packages.py": (
                "from app import shared_state\n"
                "from app.routers import admin_health\n"
                "from app.routers.admin_health import router\n"
                "import app.routers.admin_health\n"
            )
        },
    )

    assert _run(root) == [], "a package submodule import was flagged"


def test_unknowable_export_sets_are_skipped_not_reported(tmp_path: Path) -> None:
    """Star imports, module ``__getattr__`` and ``globals()`` writes are opaque.

    In each case the real export set is not in the syntax tree.  Skipping costs
    one missed catch; reporting would block every merge, which is strictly
    worse than the defect this guard exists for.
    """
    root = _tree(
        tmp_path,
        {
            "star_source.py": "WHATEVER = 1\n",
            "star.py": "from app.star_source import *  # noqa: F403\n",
            "lazy.py": "def __getattr__(name):\n    return name\n",
            "dynamic.py": (
                "for _name in ('A', 'B'):\n"
                "    globals()[_name] = _name\n"
            ),
        },
        {
            "test_opaque.py": (
                "from app.star import ANYTHING_AT_ALL\n"
                "from app.lazy import SYNTHESIZED\n"
                "from app.dynamic import A\n"
            )
        },
    )

    assert _run(root) == [], "an unknowable export set was reported instead of skipped"


def test_asserted_import_failures_are_not_reported(tmp_path: Path) -> None:
    """An import whose failure IS the assertion must never be flagged.

    ``test_invariants.py`` legitimately pins a retired symbol with
    ``with pytest.raises(ImportError): from app.release_scoring import
    AUTHORIZED_TEST_DELETIONS``. Reporting that would make this guard
    un-landable on the current tree, so the tolerated contexts are explicit.
    """
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {
            "test_tolerated.py": (
                "import contextlib\n"
                "\n"
                "import pytest\n"
                "\n"
                "\n"
                "def test_retired_symbol_stays_retired():\n"
                "    with pytest.raises(ImportError):\n"
                "        from app.target import RETIRED  # noqa: F401\n"
                "\n"
                "\n"
                "def test_optional_dependency():\n"
                "    try:\n"
                "        from app.target import OPTIONAL\n"
                "    except ImportError:\n"
                "        OPTIONAL = None\n"
                "\n"
                "    return OPTIONAL\n"
                "\n"
                "\n"
                "def test_suppressed():\n"
                "    with contextlib.suppress(ImportError):\n"
                "        from app.target import SUPPRESSED  # noqa: F401\n"
            )
        },
    )

    assert _run(root) == [], "an import whose failure is asserted was reported"


def test_asserted_failure_tolerance_does_not_leak_to_siblings(
    tmp_path: Path,
) -> None:
    """Tolerance is scoped to the guarded block, not to the rest of the file."""
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {
            "test_scope.py": (
                "import pytest\n"
                "\n"
                "\n"
                "def test_mixed():\n"
                "    with pytest.raises(ImportError):\n"
                "        from app.target import RETIRED  # noqa: F401\n"
                "\n"
                "    from app.target import ALSO_GONE\n"
                "\n"
                "    return ALSO_GONE\n"
            )
        },
    )

    violations = _run(root)
    assert [violation.statement for violation in violations] == [
        "from app.target import ALSO_GONE"
    ], "tolerance leaked outside the guarded block (or swallowed the real one)"


def test_relative_and_foreign_imports_are_ignored(tmp_path: Path) -> None:
    """Only the ``app`` package is resolved; nothing else is second-guessed."""
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {
            "test_foreign.py": (
                "from . import sibling\n"
                "from .helpers import thing\n"
                "from application_layer import other\n"
                "from apparel import nope\n"
                "import apps.registry\n"
                "from app.target import *  # noqa: F403\n"
            )
        },
    )

    assert _run(root) == [], "a non-app or relative import was resolved anyway"


def test_unparseable_test_file_is_skipped(tmp_path: Path) -> None:
    """A syntax error is pytest's failure to report, not this guard's."""
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {"test_broken.py": "def test_x(:\n    pass\n"},
    )

    assert _run(root) == [], "an unparseable file produced a violation"


# --------------------------------------------------------------------------
# The script is runnable on its own, the way check_size_limits.py is.
# --------------------------------------------------------------------------


def test_cli_exits_nonzero_and_names_the_offender(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {
            "test_cli.py": (
                "def test_local():\n"
                "    from app.target import GONE\n"
                "\n"
                "    return GONE\n"
            )
        },
    )

    result = subprocess.run(
        [sys.executable, str(CHECKER_PATH), "--repo-root", str(root), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, f"CLI exited {result.returncode}: {result.stderr}"
    payload = json.loads(result.stdout)
    assert len(payload) == 1, result.stdout
    assert payload[0]["kind"] == "stale-name"
    assert payload[0]["lineno"] == 2
    assert payload[0]["test_path"] == "backend/testing/test_cli.py"


def test_cli_exits_zero_on_a_clean_tree(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"target.py": "PRESENT = 1\n"},
        {"test_cli.py": "from app.target import PRESENT\n"},
    )

    result = subprocess.run(
        [sys.executable, str(CHECKER_PATH), "--repo-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"CLI exited {result.returncode}: {result.stdout}"
    assert "resolve" in result.stdout
