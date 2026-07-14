"""Tests for the declarative required-runtime-env manifest (Slice 1).

Covers:
- manifest well-formedness (every entry has a valid scope, presence-only);
- the GITHUB_PAT_ENCRYPTION_KEY entry references the shared name constant
  rather than a duplicated string literal;
- the manifest walker _verify_required_env() raises for an absent in-scope var
  and passes otherwise, honouring runtime scope (K_REVISION);
- a structural single-funnel guard (design §3.5): no boot-path `raise
  RuntimeError` in main.py / lifecycle.py is guarded by an un-manifested
  os.getenv() presence check for a variable the manifest owns;
- regression: Cloud-Run-missing-key path (K_REVISION set, key absent) still
  raises RuntimeError.
"""

import ast
from pathlib import Path

import pytest

from app.field_encryption import ENCRYPTION_KEY_ENV
from app.required_runtime_env import (
    REQUIRED_RUNTIME_ENV,
    VALID_SCOPES,
    _verify_required_env,
    active_scopes,
)

CLOUD_RUN_ENV = "K_REVISION"


def _clear_env(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Manifest well-formedness
# --------------------------------------------------------------------------- #


def test_every_entry_has_a_valid_scope():
    assert REQUIRED_RUNTIME_ENV, "manifest must not be empty"
    for req in REQUIRED_RUNTIME_ENV:
        assert req.scope in VALID_SCOPES, (
            f"{req.name} has invalid scope {req.scope!r}; "
            f"must be one of {sorted(VALID_SCOPES)}"
        )


def test_every_entry_is_presence_only():
    # Slice 1 is presence-only; the walker never reads values.
    for req in REQUIRED_RUNTIME_ENV:
        assert req.presence_only is True


def test_env_requirement_is_frozen():
    from dataclasses import FrozenInstanceError

    req = REQUIRED_RUNTIME_ENV[0]
    with pytest.raises(FrozenInstanceError):
        req.name = "MUTATED"  # type: ignore[misc]


def test_encryption_key_entry_uses_shared_constant():
    entries = [r for r in REQUIRED_RUNTIME_ENV if r.name == ENCRYPTION_KEY_ENV]
    assert len(entries) == 1, "expected exactly one GITHUB_PAT_ENCRYPTION_KEY entry"
    entry = entries[0]
    # It must be the imported constant, not a duplicated literal.
    assert entry.name is ENCRYPTION_KEY_ENV
    assert entry.scope == "cloud_run"
    assert entry.reason, "entry must carry a human reason"


# --------------------------------------------------------------------------- #
# active_scopes()
# --------------------------------------------------------------------------- #


def test_active_scopes_without_cloud_run(monkeypatch):
    _clear_env(monkeypatch, CLOUD_RUN_ENV)
    scopes = active_scopes()
    assert "always" in scopes
    assert "cloud_run" not in scopes


def test_active_scopes_with_cloud_run(monkeypatch):
    monkeypatch.setenv(CLOUD_RUN_ENV, "app-00042-abc")
    scopes = active_scopes()
    assert scopes == frozenset({"always", "cloud_run"})


# --------------------------------------------------------------------------- #
# _verify_required_env() behaviour
# --------------------------------------------------------------------------- #


def test_cloud_run_missing_key_raises(monkeypatch):
    """Regression: K_REVISION set + GITHUB_PAT_ENCRYPTION_KEY absent -> raise."""
    monkeypatch.setenv(CLOUD_RUN_ENV, "app-00042-abc")
    _clear_env(monkeypatch, ENCRYPTION_KEY_ENV)
    with pytest.raises(RuntimeError) as exc:
        _verify_required_env()
    # Error message must name the missing variable so the operator can fix it.
    assert ENCRYPTION_KEY_ENV in str(exc.value)


def test_cloud_run_present_key_passes(monkeypatch):
    monkeypatch.setenv(CLOUD_RUN_ENV, "app-00042-abc")
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, "any-non-empty-value")
    # Presence-only: value need not be a valid Fernet key here.
    assert _verify_required_env() is None


def test_non_cloud_run_missing_key_passes(monkeypatch):
    """cloud_run-scoped entries are inactive when K_REVISION is unset."""
    _clear_env(monkeypatch, CLOUD_RUN_ENV, ENCRYPTION_KEY_ENV)
    assert _verify_required_env() is None


def test_presence_only_empty_string_treated_absent(monkeypatch):
    monkeypatch.setenv(CLOUD_RUN_ENV, "app-00042-abc")
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, "")
    with pytest.raises(RuntimeError):
        _verify_required_env()


# --------------------------------------------------------------------------- #
# Structural single-funnel guard (design §3.5)
# --------------------------------------------------------------------------- #

# Env vars used purely as runtime *scope selectors* (not "required to boot"),
# so a raise gated by their presence is legitimate and not a manifest bypass.
_SCOPE_SIGNAL_ENV = {CLOUD_RUN_ENV}

# Resolve env-name constants that boot code may pass to os.getenv by symbol.
_KNOWN_ENV_CONSTANTS = {"ENCRYPTION_KEY_ENV": ENCRYPTION_KEY_ENV}

def _boot_module_paths() -> list[Path]:
    """Resolve boot-path source files via the imported modules' __file__.

    Robust to differing repo layouts (developer checkout vs the Docker test
    image where the package root is mounted at /backend).
    """
    import app.lifecycle
    import app.main

    return [Path(app.main.__file__), Path(app.lifecycle.__file__)]


def _env_name_from_call(node: ast.AST) -> str | None:
    """If node is os.getenv(...) / os.environ.get(...), return the env name literal."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_getenv = (
        isinstance(func, ast.Attribute)
        and func.attr in ("getenv", "get")
        and bool(node.args)
    )
    if not is_getenv:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name) and arg.id in _KNOWN_ENV_CONSTANTS:
        return _KNOWN_ENV_CONSTANTS[arg.id]
    return None


def _presence_checked_env_names(test_node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(test_node):
        name = _env_name_from_call(sub)
        if name is not None:
            names.add(name)
    return names


def _body_raises_runtime_error(nodes: list[ast.stmt]) -> bool:
    for stmt in nodes:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise):
                exc = sub.exc
                if (
                    isinstance(exc, ast.Call)
                    and isinstance(exc.func, ast.Name)
                    and exc.func.id == "RuntimeError"
                ):
                    return True
                if isinstance(exc, ast.Name) and exc.id == "RuntimeError":
                    return True
    return False


def test_no_unmanifested_env_presence_guarded_raise():
    """Manifested boot env vars must funnel through the manifest.

    Structural guard: if any `if <os.getenv(VAR) presence check>: ... raise
    RuntimeError` exists in a boot module for a variable the manifest owns, the
    single source of truth has been bypassed. (Scope-selector vars like
    K_REVISION are allowed to gate a raise.)
    """
    manifested = {req.name for req in REQUIRED_RUNTIME_ENV}
    offenders: list[str] = []
    for path in _boot_module_paths():
        rel = path.name
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            env_names = _presence_checked_env_names(node.test)
            env_names -= _SCOPE_SIGNAL_ENV
            if not env_names:
                continue
            if not _body_raises_runtime_error(node.body):
                continue
            leaked = env_names & manifested
            offenders.extend(
                f"{rel}: raise guarded by un-funneled getenv({name})"
                for name in sorted(leaked)
            )
    assert not offenders, (
        "Manifested env vars must be presence-checked via REQUIRED_RUNTIME_ENV, "
        "not by an ad-hoc os.getenv guard in the boot path:\n  "
        + "\n  ".join(offenders)
    )
