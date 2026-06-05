"""Tests for migration 0151 — the computejob renumber-skip reconciliation.

0151's job is pure guard-and-replay: when the live DB is missing ``computejob``
(because the back-merge renumber left ``alembic_version`` already past 0143's
revision id), replay 0143's ``upgrade()`` verbatim; when the table exists
(every fresh/healthy DB), do nothing. The DDL itself is 0143's — exercised by
every fresh ``alembic upgrade head`` (the Postgres harness) and covered by the
model-coverage invariants — so these tests pin the *dispatch* logic: the guard
reads the right table name, the replay actually invokes 0143, and the no-op
path never touches DDL.

Isolated in the test_deploy_smoketest style: the alembic ``op`` proxy is
monkeypatched so no real DDL runs.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MIGRATIONS_DIR = Path(__file__).parent.parent / "alembic" / "versions"


def _load_migration(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, MIGRATIONS_DIR / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _m0151():
    return _load_migration(
        "0151_reconcile_computejob_table.py", "_test_migration_0151"
    )


def test_chain_identity():
    m = _m0151()
    assert m.revision == "0151"
    assert m.down_revision == "0150"


def test_noop_when_computejob_exists(monkeypatch):
    """Healthy/fresh DB: table present -> 0143 must NOT be replayed."""
    m = _m0151()

    inspector = MagicMock()
    inspector.get_table_names.return_value = ["task", "project", "computejob"]
    monkeypatch.setattr(m.sa, "inspect", lambda bind: inspector)
    monkeypatch.setattr(m, "op", MagicMock())

    replayed = MagicMock()
    monkeypatch.setattr(m, "_load_0143", replayed)

    m.upgrade()

    replayed.assert_not_called()


def test_replays_0143_when_computejob_missing(monkeypatch):
    """Renumber-skip DB: table absent -> 0143's upgrade() is invoked."""
    m = _m0151()

    inspector = MagicMock()
    inspector.get_table_names.return_value = ["task", "project"]
    monkeypatch.setattr(m.sa, "inspect", lambda bind: inspector)
    monkeypatch.setattr(m, "op", MagicMock())

    loaded_0143 = MagicMock()
    monkeypatch.setattr(m, "_load_0143", MagicMock(return_value=loaded_0143))

    m.upgrade()

    loaded_0143.upgrade.assert_called_once_with()


def test_load_0143_resolves_the_real_module():
    """The path-based import must land on the genuine 0143 migration: right
    revision id, callable upgrade. Catches a rename/move of 0143 breaking the
    replay at deploy time rather than in CI."""
    m = _m0151()
    loaded = m._load_0143()
    assert loaded.revision == "0143"
    assert loaded.down_revision == "0142"
    assert callable(loaded.upgrade)
    # The replay must create the exact table the drift guard demanded.
    import inspect as py_inspect

    assert '"computejob"' in py_inspect.getsource(loaded.upgrade)


def test_downgrade_is_forward_only():
    m = _m0151()
    with pytest.raises(NotImplementedError):
        m.downgrade()
