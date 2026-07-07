"""Tests for the reverse-dependency resolver in ``impacted_tests.py``.

The resolver powers the merge-time impacted-test gate: it maps a set of changed
repo-relative paths to the ``backend/testing/test_*.py`` files that exercise the
changed modules (by import or by string mock-patch target), plus any directly
changed/added test file. These tests build a hermetic fixture tree under
``tmp_path`` and assert the mapping, boundary handling, and edge cases.
"""

import io
import os

from testing import impacted_tests


def _write(root: str, rel: str, content: str = "") -> None:
    abs_path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_tree(root: str) -> None:
    """A minimal repo fixture: a couple of app modules and testing/ dir."""
    _write(root, "backend/app/foo.py", "def bar():\n    return 1\n")
    _write(root, "backend/app/foobar.py", "def baz():\n    return 2\n")
    _write(root, "backend/app/routers/agent.py", "def handler():\n    return 3\n")
    os.makedirs(os.path.join(root, "backend", "testing"), exist_ok=True)


def test_app_module_for_path():
    assert impacted_tests.app_module_for_path("backend/app/foo.py") == "app.foo"
    assert (
        impacted_tests.app_module_for_path("backend/app/routers/agent.py")
        == "app.routers.agent"
    )
    # Non-app / non-.py paths yield None.
    assert impacted_tests.app_module_for_path("backend/testing/test_foo.py") is None
    assert impacted_tests.app_module_for_path("agent/src/index.ts") is None
    assert impacted_tests.app_module_for_path("backend/app/foo.txt") is None


def test_changed_module_maps_to_importing_test(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/test_foo_user.py",
        "from app.foo import bar\n\n\ndef test_x():\n    assert bar() == 1\n",
    )
    result = impacted_tests.resolve_impacted(["backend/app/foo.py"], root)
    assert result == ["backend/testing/test_foo_user.py"]


def test_changed_module_maps_to_mock_patch_string_target(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # The test never imports app.foo — it references it only as a string
    # mock-patch target. The reverse-dep scan must still select it.
    _write(
        root,
        "backend/testing/test_foo_patch.py",
        'from unittest.mock import patch\n\n\n'
        'def test_x():\n'
        '    with patch("app.foo.bar", return_value=9):\n'
        '        pass\n',
    )
    result = impacted_tests.resolve_impacted(["backend/app/foo.py"], root)
    assert result == ["backend/testing/test_foo_patch.py"]


def test_directly_changed_test_file_included_as_itself(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    _write(root, "backend/testing/test_direct.py", "def test_ok():\n    assert True\n")
    result = impacted_tests.resolve_impacted(["backend/testing/test_direct.py"], root)
    assert result == ["backend/testing/test_direct.py"]


def test_nested_app_module_resolves_both_import_forms(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/test_agent_dotted.py",
        "import app.routers.agent\n\n\ndef test_a():\n    assert app.routers.agent\n",
    )
    _write(
        root,
        "backend/testing/test_agent_from_parent.py",
        "from app.routers import agent\n\n\ndef test_b():\n    assert agent\n",
    )
    result = impacted_tests.resolve_impacted(
        ["backend/app/routers/agent.py"], root
    )
    assert result == [
        "backend/testing/test_agent_dotted.py",
        "backend/testing/test_agent_from_parent.py",
    ]


def test_module_boundary_does_not_match_longer_sibling(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # This test references only app.foobar, NOT app.foo. Changing app.foo must
    # not select it (prefix-collision guard).
    _write(
        root,
        "backend/testing/test_foobar_only.py",
        "from app.foobar import baz\n\n\ndef test_x():\n    assert baz() == 2\n",
    )
    result = impacted_tests.resolve_impacted(["backend/app/foo.py"], root)
    assert result == []
    # And changing app.foobar DOES select it (sanity: the guard isn't over-broad).
    result_bar = impacted_tests.resolve_impacted(["backend/app/foobar.py"], root)
    assert result_bar == ["backend/testing/test_foobar_only.py"]


def test_deleted_or_nonexistent_paths_are_skipped(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # A changed app module with no referencing test yields nothing; a changed
    # test path that isn't on disk (a deletion) is not emitted; neither crashes.
    result = impacted_tests.resolve_impacted(
        ["backend/app/foo.py", "backend/testing/test_gone.py"], root
    )
    assert result == []


def test_non_backend_and_non_app_paths_ignored(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/test_foo_user.py",
        "from app.foo import bar\n\n\ndef test_x():\n    assert bar()\n",
    )
    # Frontend / agent / docs changes contribute no impacted backend tests.
    result = impacted_tests.resolve_impacted(
        ["admin-ui/src/App.tsx", "agent/src/index.ts", "docs/API_REFERENCE.md"],
        root,
    )
    assert result == []


def test_empty_input_yields_empty_output(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    assert impacted_tests.resolve_impacted([], root) == []


def test_dedup_and_stable_sort(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # One test references BOTH changed modules — it must appear exactly once.
    _write(
        root,
        "backend/testing/test_both.py",
        "from app.foo import bar\nimport app.routers.agent\n",
    )
    _write(
        root,
        "backend/testing/test_alpha.py",
        "from app.foo import bar\n",
    )
    result = impacted_tests.resolve_impacted(
        ["backend/app/foo.py", "backend/app/routers/agent.py"], root
    )
    # Deduped (test_both counted once) and sorted lexicographically.
    assert result == [
        "backend/testing/test_alpha.py",
        "backend/testing/test_both.py",
    ]


def test_missing_testing_dir_is_graceful(tmp_path):
    root = str(tmp_path)
    # No backend/testing at all — the scan must be skipped, not raise.
    _write(root, "backend/app/foo.py", "def bar():\n    return 1\n")
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []


def test_main_reads_stdin_and_writes_stdout(tmp_path, monkeypatch, capsys):
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/test_foo_user.py",
        "from app.foo import bar\n",
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("backend/app/foo.py\nadmin-ui/src/App.tsx\n")
    )
    rc = impacted_tests.main(["--repo-root", root])
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["backend/testing/test_foo_user.py"]


def test_main_limit_defers_overflow_to_stderr(tmp_path, capsys):
    root = str(tmp_path)
    _make_tree(root)
    for i in range(3):
        _write(
            root,
            f"backend/testing/test_mod{i}.py",
            "from app.foo import bar\n",
        )
    rc = impacted_tests.main(
        ["backend/app/foo.py", "--limit", "2", "--repo-root", root]
    )
    assert rc == 0
    captured = capsys.readouterr()
    stdout = captured.out.splitlines()
    stderr = captured.err.splitlines()
    # First 2 (sorted) emitted; the 3rd deferred to CI on stderr.
    assert stdout == [
        "backend/testing/test_mod0.py",
        "backend/testing/test_mod1.py",
    ]
    assert stderr == ["deferred-to-CI: backend/testing/test_mod2.py"]
