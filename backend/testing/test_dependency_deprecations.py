"""
Deprecation-warning guard tests.

Verifies that the pytest filterwarnings configuration in pyproject.toml is active
and will turn DeprecationWarnings from core runtime dependencies into errors.

Motivating case: httpx 0.27 deprecated the `app=` shortcut; the test suite never
surfaced that warning; httpx 0.28 removed the API entirely, which would have been
a hard CI failure with no prior signal.

Run standalone (no DB, no conftest):
    pytest testing/test_dependency_deprecations.py -v --noconftest
"""

import re
import warnings
from pathlib import Path

import pytest

from testing.fixgate_attribution import format_offender_marker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GUARDED_MODULES = [
    "httpx",
    "starlette",
    "fastapi",
    "sqlalchemy",
    "sqlmodel",
    "pydantic",
    "alembic",
    "uvicorn",
    "asyncpg",
    "jwt",
]


def _active_error_filters_for_module(module_name: str) -> list:
    """Return warning filters that turn DeprecationWarning from module_name into errors."""
    results = []
    for action, _msg, category, mod_pattern, _lineno in warnings.filters:
        if action != "error":
            continue
        if not (
            category is DeprecationWarning
            or category is PendingDeprecationWarning
            or (isinstance(category, type) and issubclass(DeprecationWarning, category))
        ):
            continue
        if mod_pattern is None:
            # A global error::DeprecationWarning filter covers everything.
            results.append((action, category, mod_pattern))
            continue
        pattern_str = mod_pattern.pattern if hasattr(mod_pattern, "pattern") else str(mod_pattern)
        if re.match(pattern_str, module_name):
            results.append((action, category, mod_pattern))
    return results


# ---------------------------------------------------------------------------
# Filter-active tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _GUARDED_MODULES)
def test_deprecation_warning_filter_is_active_for_module(module_name):
    """Each core dependency must have an active error filter for DeprecationWarning.

    If this test fails the filterwarnings list in [tool.pytest.ini_options] in
    pyproject.toml is missing an entry for this module.
    """
    active = _active_error_filters_for_module(module_name)
    assert active, (
        f"No active filter turns DeprecationWarning from '{module_name}' into an error. "
        f"Add 'error::DeprecationWarning:{module_name}' to "
        "[tool.pytest.ini_options].filterwarnings in backend/pyproject.toml."
    )


# ---------------------------------------------------------------------------
# Behavioural canary: warn_explicit lets us inject a warning that appears to
# originate from any module without requiring the package to be installed.
# ---------------------------------------------------------------------------


def test_httpx_deprecation_warning_raises_as_error():
    """A DeprecationWarning emitted by the httpx module must raise, not be silenced.

    This is the primary canary: if the filterwarnings guard is removed, this test
    will fail because warnings.warn_explicit will no longer raise.
    """
    with pytest.raises(DeprecationWarning, match="canary"):
        warnings.warn_explicit(
            "canary: simulated httpx deprecation (e.g. app= shortcut)",
            DeprecationWarning,
            filename="httpx/_client.py",
            lineno=1,
            module="httpx",
        )


def test_pydantic_deprecation_warning_raises_as_error():
    """A DeprecationWarning emitted by pydantic must raise, not be silenced."""
    with pytest.raises(DeprecationWarning, match="canary"):
        warnings.warn_explicit(
            "canary: simulated pydantic deprecation",
            DeprecationWarning,
            filename="pydantic/main.py",
            lineno=1,
            module="pydantic",
        )


def test_sqlalchemy_deprecation_warning_raises_as_error():
    """A DeprecationWarning emitted by sqlalchemy must raise, not be silenced."""
    with pytest.raises(DeprecationWarning, match="canary"):
        warnings.warn_explicit(
            "canary: simulated sqlalchemy deprecation",
            DeprecationWarning,
            filename="sqlalchemy/orm/session.py",
            lineno=1,
            module="sqlalchemy",
        )


def test_pending_deprecation_warning_also_raises_for_httpx():
    """PendingDeprecationWarning from httpx must also raise (pre-removal signal)."""
    with pytest.raises(PendingDeprecationWarning, match="canary"):
        warnings.warn_explicit(
            "canary: simulated httpx pending deprecation",
            PendingDeprecationWarning,
            filename="httpx/_client.py",
            lineno=1,
            module="httpx",
        )


# ---------------------------------------------------------------------------
# pyproject.toml structural check (also present in test_invariants.py;
# duplicated here so this file is self-contained for targeted runs).
# ---------------------------------------------------------------------------


def test_no_pydantic_v1_idioms_in_app_source():
    """backend/app must not contain pydantic-v1 idioms that break under pydantic v2.

    Scans every .py file under backend/app for patterns that are either removed
    or deprecated in pydantic v2: the @validator/@root_validator decorators, the
    pydantic.v1 compat shim import, and direct imports of the old ``validator``
    symbol.  ``payload_schemas._PYDANTIC_V2_ONLY`` being importable is the
    companion production-code anchor — removing it would break this test.

    Why static rather than runtime: a deprecated idiom on an untested code path
    ships silently past filterwarnings=error and becomes a hard failure at the
    next pydantic major.  A static ratchet catches reintroduction immediately
    regardless of test coverage.
    """
    import importlib

    # Verify the production-code anchor is present (fails if someone removes the
    # sentinel that documents the v2-only policy in payload_schemas).
    ps = importlib.import_module("app.payload_schemas")
    assert getattr(ps, "_PYDANTIC_V2_ONLY", None) is True, (
        "app.payload_schemas._PYDANTIC_V2_ONLY sentinel is missing. "
        "Do not remove it — it anchors the pydantic-v2-only policy."
    )

    app_dir = Path(__file__).resolve().parents[1] / "app"
    assert app_dir.is_dir(), f"backend/app directory not found at {app_dir}"

    # Unambiguous pydantic v1 patterns.  Each tuple is (human label, regex).
    v1_patterns = [
        ("@validator( deprecated decorator", re.compile(r"^\s*@validator\s*\(", re.MULTILINE)),
        ("@root_validator deprecated decorator", re.compile(r"^\s*@root_validator\b", re.MULTILINE)),
        ("pydantic.v1 compat shim import", re.compile(r"\bfrom\s+pydantic\.v1\b")),
        ("import of deprecated validator symbol", re.compile(r"\bfrom\s+pydantic\s+import\b[^#\n]*\bvalidator\b")),
    ]

    violations: list[str] = []
    for py_file in sorted(app_dir.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        for label, pattern in v1_patterns:
            if pattern.search(source):
                violations.append(f"{py_file.relative_to(app_dir.parent.parent)}: {label}")

    assert not violations, (
        "Pydantic v1 idioms found in backend/app — replace with v2 equivalents:\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\n"
        + format_offender_marker(violations)
    )


def test_sqlmodel_table_model_rejects_unknown_constructor_kwargs():
    """check_model_kwargs must raise TypeError for fields that no longer exist.

    SQLModel table=True models silently discard unknown constructor keywords,
    so a stale test that passes a removed column name still 'passes'.
    check_model_kwargs is the static ratchet: call it whenever you construct a
    model in a test and the constructor will surface unknown fields immediately.

    Canary: if check_model_kwargs is removed or weakened, this test fails.
    Production-code anchor: app.models.check_model_kwargs (exercises the
    model_fields introspection on a real table=True model).
    """
    from app.models import Project, check_model_kwargs

    # A field that was removed (Project never had runtime_config_json).
    with pytest.raises(TypeError, match="unknown field"):
        check_model_kwargs(Project, {"runtime_config_json": "stale-value"})

    # Multiple unknown fields are all reported together.
    with pytest.raises(TypeError, match="unknown field"):
        check_model_kwargs(Project, {"removed_col_a": 1, "removed_col_b": 2})

    # Known field does NOT raise.
    check_model_kwargs(Project, {"name": "valid-project-name"})


def test_pyproject_toml_filterwarnings_present():
    """pyproject.toml must declare a filterwarnings list under [tool.pytest.ini_options]."""
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            pytest.skip("tomllib/tomli not available on Python < 3.11")
            return

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert pyproject_path.exists(), f"pyproject.toml not found at {pyproject_path}"

    with open(pyproject_path, "rb") as fh:
        config = tomllib.load(fh)

    filterwarnings = (
        config.get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("filterwarnings", [])
    )
    assert filterwarnings, (
        "No filterwarnings list found in [tool.pytest.ini_options] in pyproject.toml. "
        "Add error::DeprecationWarning:<pkg> entries for core runtime dependencies."
    )

    required = "error::DeprecationWarning:httpx"
    assert required in filterwarnings, (
        f"'{required}' is missing from [tool.pytest.ini_options].filterwarnings "
        "in pyproject.toml. This entry is the primary guard against silent httpx "
        "deprecation regressions."
    )
