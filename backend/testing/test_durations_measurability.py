"""
Standalone guard: the durations coverage ratchet's exemption must stay derived.

``test_test_durations_freshness.py`` asserts that every backend test module
carries at least one entry in ``backend/.test_durations``. ``--store-durations``
writes one entry per EXECUTED test, so a module pytest never executes can never
satisfy that -- and an unsatisfiable clause in a promotion gate is debt every
future candidate inherits and none can discharge.

``testing/durations_measurability.py`` carves those modules out. The danger in
any such carve-out is that it quietly becomes a way to skip the gate, so these
pin the three properties that keep it honest: every exemption is evidenced in
the excluded module's own source, the exempt set stays a handful rather than a
class, and an input that cannot be read exempts nothing at all.

Runs under --noconftest (no DB, no conftest fixtures).
"""
import ast
from pathlib import Path

from testing.durations_measurability import (
    ZERO_TEST_MARKER,
    deselected_markers,
    module_pytestmarks,
    unmeasurable_reason,
)

BACKEND_ROOT = Path(__file__).parents[1]
PYPROJECT_PATH = BACKEND_ROOT / "pyproject.toml"


def _roster() -> dict[str, Path]:
    """Return the backend test roster keyed by suite-relative path."""
    testing_dir = BACKEND_ROOT / "testing"
    if not testing_dir.is_dir():
        return {}
    return {
        path.relative_to(BACKEND_ROOT).as_posix(): path
        for path in sorted(testing_dir.glob("test_*.py"))
    }


def _exempt() -> dict[str, str]:
    """Return roster modules the ratchet exempts, mapped to the stated reason."""
    deselected = deselected_markers(PYPROJECT_PATH)
    reasons = {}
    for file_key, path in _roster().items():
        reason = unmeasurable_reason(path, deselected)
        if reason is not None:
            reasons[file_key] = reason
    return reasons


def test_deselected_markers_reads_the_repository_addopts() -> None:
    """The real config, not a restatement of it, decides what is deselected."""
    markers = deselected_markers(PYPROJECT_PATH)
    # Literal pin. If someone removes ``-m 'not integration'`` from addopts, the
    # suite starts running those modules, they become measurable, and the
    # exemption must disappear with it -- so this failing is the correct alarm.
    assert "integration" in markers, (
        "backend/pyproject.toml addopts no longer deselects the 'integration' "
        "marker. If integration tests now run by default they are measurable "
        "and must carry durations entries; drop the exemption rather than "
        "widening it."
    )


def test_every_exemption_carries_evidence_in_its_own_source() -> None:
    """No module is exempt by name -- each one declares why, in its own file."""
    roster = _roster()
    deselected = deselected_markers(PYPROJECT_PATH)

    for file_key, reason in _exempt().items():
        source = roster[file_key].read_text(encoding="utf-8")
        declares_helper = ZERO_TEST_MARKER in source
        deselected_module = bool(module_pytestmarks(ast.parse(source)) & deselected)
        assert declares_helper or deselected_module, (
            f"{file_key} is exempt from the durations coverage ratchet for "
            f"{reason!r}, but its own source carries neither the "
            f"intentional-zero-tests marker nor a module-level pytestmark that "
            f"addopts deselects. An exemption with no evidence in the excluded "
            f"file is a hole in the guard."
        )


def test_exempt_modules_stay_a_narrow_exception() -> None:
    """A broad addopts change must not quietly gut the coverage ratchet."""
    roster = _roster()
    if not roster:
        return
    exempt = _exempt()
    fraction = len(exempt) / len(roster)
    # Adding, say, ``-m 'not asyncio'`` to addopts would exempt hundreds of
    # modules at once and leave the ratchet asserting almost nothing, while
    # every individual exemption still looked individually justified.
    assert fraction <= 0.02, (
        f"{len(exempt)} of {len(roster)} roster modules ({fraction:.1%}) are "
        f"exempt from the durations coverage ratchet. The exemption covers a "
        f"handful of structurally unmeasurable modules, not a class of them. "
        f"Exempt: {sorted(exempt)[:10]}"
    )


def test_deselected_markers_is_fail_closed_on_unusable_config(tmp_path: Path) -> None:
    """An unreadable config exempts nothing; it must not widen the blind spot."""
    assert deselected_markers(tmp_path / "absent.toml") == frozenset()

    malformed = tmp_path / "bad.toml"
    malformed.write_text("[tool.pytest\n", encoding="utf-8")
    assert deselected_markers(malformed) == frozenset()

    no_addopts = tmp_path / "none.toml"
    no_addopts.write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['testing']\n", encoding="utf-8"
    )
    assert deselected_markers(no_addopts) == frozenset()

    no_selector = tmp_path / "plain.toml"
    no_selector.write_text(
        '[tool.pytest.ini_options]\naddopts = "--reruns 2"\n', encoding="utf-8"
    )
    assert deselected_markers(no_selector) == frozenset()

    # A selector too involved to read literally yields nothing rather than a
    # guess, so those modules stay demanded.
    grouped = tmp_path / "grouped.toml"
    grouped.write_text(
        "[tool.pytest.ini_options]\naddopts = \"-m 'not (slow or flaky)'\"\n",
        encoding="utf-8",
    )
    assert deselected_markers(grouped) == frozenset()


def test_deselected_markers_reads_every_spelling_pytest_accepts(
    tmp_path: Path,
) -> None:
    """Separate argument, attached argument, several terms, and a list value."""
    combined = tmp_path / "combined.toml"
    combined.write_text(
        '[tool.pytest.ini_options]\n'
        "addopts = \"-m 'not integration and not slow' -q\"\n",
        encoding="utf-8",
    )
    assert deselected_markers(combined) == frozenset({"integration", "slow"})

    # The attached form only reaches pytest as one selector when it is
    # quoted: unquoted, the shell splits `-mnot integration` into `-m not`
    # plus a stray positional, and pytest deselects nothing. Read it the
    # same way rather than being cleverer than the shell.
    attached = tmp_path / "attached.toml"
    attached.write_text(
        '[tool.pytest.ini_options]\naddopts = "-m\'not integration\'"\n',
        encoding="utf-8",
    )
    assert deselected_markers(attached) == frozenset({"integration"})

    unquoted = tmp_path / "unquoted.toml"
    unquoted.write_text(
        '[tool.pytest.ini_options]\naddopts = "-mnot integration"\n',
        encoding="utf-8",
    )
    assert deselected_markers(unquoted) == frozenset()

    listed = tmp_path / "listed.toml"
    listed.write_text(
        "[tool.pytest.ini_options]\naddopts = ['-m', 'not integration']\n",
        encoding="utf-8",
    )
    assert deselected_markers(listed) == frozenset({"integration"})


def test_unmeasurable_reason_is_fail_closed_on_unreadable_source(
    tmp_path: Path,
) -> None:
    """Source we cannot read or parse stays demanded, never excused."""
    deselected = frozenset({"integration"})

    assert unmeasurable_reason(tmp_path / "absent.py", deselected) is None

    unparseable = tmp_path / "test_broken.py"
    unparseable.write_text("def test_x( :\n", encoding="utf-8")
    assert unmeasurable_reason(unparseable, deselected) is None

    ordinary = tmp_path / "test_ordinary.py"
    ordinary.write_text("def test_x():\n    pass\n", encoding="utf-8")
    assert unmeasurable_reason(ordinary, deselected) is None


def test_unmeasurable_reason_exempts_only_what_the_suite_really_skips(
    tmp_path: Path,
) -> None:
    """A marker exempts a module only while addopts actually deselects it."""
    marked = tmp_path / "test_marked.py"
    marked.write_text(
        "import pytest\n\npytestmark = pytest.mark.integration\n\n"
        "def test_x():\n    pass\n",
        encoding="utf-8",
    )
    # Same file, same marker: exempt only when the config says it never runs.
    assert unmeasurable_reason(marked, frozenset()) is None
    assert "integration" in (
        unmeasurable_reason(marked, frozenset({"integration"})) or ""
    )

    helper = tmp_path / "test_helper.py"
    helper.write_text(
        f'"""Shared assertions."""\n\n{ZERO_TEST_MARKER} -- imported helpers\n\n'
        "def assert_thing(value):\n    assert value\n",
        encoding="utf-8",
    )
    # The helper declaration stands on its own, with no config involved.
    assert unmeasurable_reason(helper, frozenset()) is not None


def test_module_pytestmarks_reads_both_spellings() -> None:
    """Single marker, list of markers, and the called form all resolve."""
    single = ast.parse("import pytest\npytestmark = pytest.mark.integration\n")
    assert module_pytestmarks(single) == frozenset({"integration"})

    listed = ast.parse(
        "import pytest\npytestmark = [pytest.mark.integration, pytest.mark.slow]\n"
    )
    assert module_pytestmarks(listed) == frozenset({"integration", "slow"})

    called = ast.parse(
        "import pytest\npytestmark = pytest.mark.skipif(True, reason='x')\n"
    )
    assert module_pytestmarks(called) == frozenset({"skipif"})

    annotated = ast.parse(
        "import pytest\npytestmark: object = pytest.mark.integration\n"
    )
    assert module_pytestmarks(annotated) == frozenset({"integration"})


def test_module_pytestmarks_does_not_guess() -> None:
    """A mark it cannot read statically leaves the module demanded."""
    # A function-level mark is not a module-level one: the module still runs.
    function_level = ast.parse(
        "import pytest\n\n@pytest.mark.integration\ndef test_x():\n    pass\n"
    )
    assert module_pytestmarks(function_level) == frozenset()

    # Computed at import time: not read, so the module stays demanded.
    computed = ast.parse("pytestmark = build_marks()\n")
    assert module_pytestmarks(computed) == frozenset()

    # A same-named attribute on something else is not the module-level hook.
    attribute = ast.parse("import pytest\nobj.pytestmark = pytest.mark.integration\n")
    assert module_pytestmarks(attribute) == frozenset()
