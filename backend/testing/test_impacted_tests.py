"""Tests for the reverse-dependency resolver in ``impacted_tests.py``.

The resolver powers the merge-time impacted-test gate: it maps a set of changed
repo-relative paths to the ``backend/testing/test_*.py`` files that exercise the
changed modules (by import or by string mock-patch target), plus any directly
changed/added test file. These tests build a hermetic fixture tree under
``tmp_path`` and assert the mapping, boundary handling, and edge cases.
"""

import io
import os

import pytest

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


def test_backend_verification_attributes_only_diff_introduced_failures():
    """A base failure is evidence, not a publication blocker."""
    current = {
        "testing.test_existing::test_old": "AssertionError: fixture failure",
        "testing.test_changed::test_new": "AssertionError: introduced failure",
    }
    baseline = {
        "testing.test_existing::test_old": "AssertionError: fixture failure",
    }

    outcome = impacted_tests.classify_backend_verification(
        runtime_available=True,
        current_exit_code=1,
        current_failures=current,
        baseline_failures=baseline,
    )

    assert outcome["state"] == "test_failure"
    assert outcome["blocking"] is True
    assert outcome["new_failures"] == {
        "testing.test_changed::test_new": "AssertionError: introduced failure",
    }
    assert "testing.test_existing::test_old" not in outcome["new_failures"]


def test_backend_verification_reports_runtime_unavailable_separately():
    outcome = impacted_tests.classify_backend_verification(
        runtime_available=False,
        current_exit_code=2,
        current_failures={},
    )

    assert outcome == {
        "state": "runtime_unavailable",
        "blocking": True,
        "new_failures": {},
    }


def test_backend_verification_junit_parser_preserves_route_keyed_detail(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite><testcase classname="testing.test_x" name="test_old">'
        '<failure message="boom">Assertion at /candidate/object at 0xABC</failure>'
        "</testcase></testsuite>",
        encoding="utf-8",
    )

    failures = impacted_tests.parse_junit_failures(report)

    assert failures == {
        "testing.test_x::test_old::failure": "Assertion at /candidate/object at 0xABC"
    }
    assert impacted_tests.attribute_new_failures(
        failures,
        {"testing.test_x::test_old::failure": "Assertion at /base/object at 0x123"},
        current_root="/candidate",
        baseline_root="/base",
    ) == {}


def test_backend_verification_uses_route_identity_when_rendered_detail_changes():
    current = {"backend/testing/test_x.py::test_old::failure": "line 99: changed"}
    baseline = {"backend/testing/test_x.py::test_old::failure": "line 12: old"}

    outcome = impacted_tests.classify_backend_verification(
        runtime_available=True,
        current_exit_code=1,
        current_failures=current,
        baseline_failures=baseline,
    )

    assert outcome["state"] == "preexisting_test_failure"
    assert outcome["blocking"] is False
    assert outcome["new_failures"] == {}
    assert outcome["suppressed_failures"] == {
        "backend/testing/test_x.py::test_old::failure": {
            "detail": "line 99: changed",
            "reason": "same route/test/failure kind failed on baseline",
        }
    }


def test_backend_verification_keeps_diff_introduced_route_blocking():
    current = {"backend/testing/test_x.py::test_new::failure": "introduced"}
    baseline = {"backend/testing/test_x.py::test_old::failure": "old"}

    outcome = impacted_tests.classify_backend_verification(
        runtime_available=True,
        current_exit_code=1,
        current_failures=current,
        baseline_failures=baseline,
    )

    assert outcome["state"] == "test_failure"
    assert outcome["blocking"] is True
    assert outcome["new_failures"] == current


def test_backend_verification_reports_base_failure_route_missing_from_current_report():
    current = impacted_tests.JUnitReport(
        failures={},
        test_routes={"backend/testing/test_other.py::test_still_present"},
    )
    baseline = impacted_tests.JUnitReport(
        failures={"backend/testing/test_old.py::test_removed::failure": "old"},
        test_routes={"backend/testing/test_old.py::test_removed"},
    )

    outcome = impacted_tests.classify_backend_verification(
        runtime_available=True,
        current_exit_code=0,
        current_failures={},
        current_report=current,
        baseline_report=baseline,
    )

    assert outcome["state"] == "baseline_failure_disappeared"
    assert outcome["blocking"] is True
    assert outcome["disappeared_failures"] == {
        "backend/testing/test_old.py::test_removed::failure": {
            "detail": "old",
            "reason": "baseline failing route absent from current report",
        }
    }


def test_backend_verification_reports_base_failure_route_skipped():
    current = impacted_tests.JUnitReport(
        failures={},
        test_routes={"backend/testing/test_old.py::test_removed"},
        skipped_routes={"backend/testing/test_old.py::test_removed"},
    )
    baseline = impacted_tests.JUnitReport(
        failures={"backend/testing/test_old.py::test_removed::failure": "old"},
        test_routes={"backend/testing/test_old.py::test_removed"},
    )

    outcome = impacted_tests.classify_backend_verification(
        runtime_available=True,
        current_exit_code=0,
        current_failures={},
        current_report=current,
        baseline_report=baseline,
    )

    assert outcome["state"] == "baseline_failure_skipped"
    assert outcome["blocking"] is True
    assert outcome["skipped_failures"] == {
        "backend/testing/test_old.py::test_removed::failure": {
            "detail": "old",
            "reason": "baseline failing route is skipped in current report",
        }
    }


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
        "from unittest.mock import patch\n\n\n"
        "def test_x():\n"
        '    with patch("app.foo.bar", return_value=9):\n'
        "        pass\n",
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
    result = impacted_tests.resolve_impacted(["backend/app/routers/agent.py"], root)
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


def test_dedup_and_relevance_rank(tmp_path):
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
    # Deduped (test_both counted once) and ranked by relevance: both sit at hop
    # 0, but test_both references TWO changed modules (n_direct=2) while
    # test_alpha references one, so test_both ranks first.
    assert result == [
        "backend/testing/test_both.py",
        "backend/testing/test_alpha.py",
    ]


def test_indirectly_reached_module_via_app_importer(tmp_path):
    """A test that imports a router (not the changed module directly) is selected.

    This covers the include_router pattern: logic helper ``mylib`` is imported by
    ``routers/my_router`` which is what the test file references. Changing ``mylib``
    must surface ``test_via_router.py`` even though it never mentions ``mylib``.
    """
    root = str(tmp_path)
    _make_tree(root)
    # The logic module being changed — no test references it directly.
    _write(root, "backend/app/mylib.py", "def helper(): pass\n")
    # A router that imports the changed module.
    _write(
        root,
        "backend/app/routers/my_router.py",
        "from app.mylib import helper\n\ndef route(): return helper()\n",
    )
    # Test file references only the router, not mylib.
    _write(
        root,
        "backend/testing/test_via_router.py",
        "from app.routers import my_router\n\ndef test_x(): assert my_router.route()\n",
    )
    result = impacted_tests.resolve_impacted(["backend/app/mylib.py"], root)
    assert result == ["backend/testing/test_via_router.py"]


def test_indirect_expansion_does_not_double_count_direct_reference(tmp_path):
    """A test that directly imports a changed module is listed exactly once."""
    root = str(tmp_path)
    _make_tree(root)
    _write(root, "backend/app/mylib.py", "def helper(): pass\n")
    # Router imports mylib; test imports BOTH mylib AND the router.
    _write(
        root,
        "backend/app/routers/my_router.py",
        "from app.mylib import helper\n",
    )
    _write(
        root,
        "backend/testing/test_double.py",
        "from app.mylib import helper\nfrom app.routers import my_router\n",
    )
    result = impacted_tests.resolve_impacted(["backend/app/mylib.py"], root)
    assert result == ["backend/testing/test_double.py"]


def test_missing_testing_dir_is_graceful(tmp_path):
    root = str(tmp_path)
    # No backend/testing at all — the scan must be skipped, not raise.
    _write(root, "backend/app/foo.py", "def bar():\n    return 1\n")
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []


def _write_surface_manifest(root: str, surfaces) -> None:
    """Write a drift_sentinel_surfaces.json with the given surface entries."""
    import json

    _write(
        root,
        "backend/testing/drift_sentinel_surfaces.json",
        json.dumps({"surfaces": surfaces}),
    )


def test_glob_to_regex_double_star_semantics():
    """`**/` spans any depth (including zero); `*` stays within one segment."""
    rx = impacted_tests._glob_to_regex("backend/app/**/*.py")
    assert rx.match("backend/app/foo.py")  # zero intermediate dirs
    assert rx.match("backend/app/routers/agent.py")  # one dir
    assert rx.match("backend/app/a/b/c.py")  # many dirs
    assert not rx.match("backend/app/foo.txt")  # extension guarded
    assert not rx.match("backend/testing/test_foo.py")  # prefix guarded
    # A single-segment '*' must not cross a '/'.
    seg = impacted_tests._glob_to_regex("backend/app/*.py")
    assert seg.match("backend/app/foo.py")
    assert not seg.match("backend/app/routers/agent.py")


def test_surface_glob_selects_drift_sentinel(tmp_path):
    """A changed app path the sentinel never imports still selects it via surface.

    This is the core gap this feature closes: a drift sentinel that AST-walks the
    whole tree (and therefore imports no `app.<module>`) is invisible to the
    dotted-import reverse-dep scan, but the surface manifest maps its guarded
    surface glob to it so any matching changed path selects it.
    """
    root = str(tmp_path)
    _make_tree(root)
    # A sentinel that imports NO app module — only a testing/ helper — so the
    # reverse-dep scan can never reach it.
    _write(
        root,
        "backend/testing/test_tree_sentinel.py",
        "from websocket_event_inventory import build\n\n\ndef test_x():\n    assert True\n",
    )
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/**/*.py",
                "sentinels": ["backend/testing/test_tree_sentinel.py"],
            }
        ],
    )
    # A newly added app module the sentinel does NOT import.
    _write(root, "backend/app/newthing.py", "def emit(): pass\n")
    result = impacted_tests.resolve_impacted(["backend/app/newthing.py"], root)
    assert result == ["backend/testing/test_tree_sentinel.py"]


def test_surface_glob_non_matching_change_does_not_select_sentinel(tmp_path):
    """A changed path outside the registered surface does NOT select the sentinel."""
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/test_tree_sentinel.py",
        "from websocket_event_inventory import build\n",
    )
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/**/*.py",
                "sentinels": ["backend/testing/test_tree_sentinel.py"],
            }
        ],
    )
    # Frontend/agent/docs changes fall outside the surface -> no sentinel.
    result = impacted_tests.resolve_impacted(
        ["admin-ui/src/App.tsx", "agent/src/index.ts", "docs/API_REFERENCE.md"], root
    )
    assert result == []


def test_surface_scoped_glob_matches_only_listed_files(tmp_path):
    """A file-scoped surface glob fires only for the exact files it names."""
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/test_event_sentinel.py",
        "from pathlib import Path\n",
    )
    _write(root, "backend/app/notification_dispatch.py", "x = 1\n")
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/notification_dispatch.py",
                "sentinels": ["backend/testing/test_event_sentinel.py"],
            }
        ],
    )
    # The registered file selects the sentinel...
    assert impacted_tests.resolve_impacted(
        ["backend/app/notification_dispatch.py"], root
    ) == ["backend/testing/test_event_sentinel.py"]
    # ...but a sibling app file the glob does not name does not.
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []


def test_surface_sentinel_deduped_with_reference_match(tmp_path):
    """A sentinel selected by BOTH import-reference and surface appears once."""
    root = str(tmp_path)
    _make_tree(root)
    # This sentinel BOTH imports app.foo AND is registered against the tree.
    _write(
        root,
        "backend/testing/test_dual.py",
        "from app.foo import bar\n\n\ndef test_x():\n    assert bar()\n",
    )
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/**/*.py",
                "sentinels": ["backend/testing/test_dual.py"],
            }
        ],
    )
    result = impacted_tests.resolve_impacted(["backend/app/foo.py"], root)
    assert result == ["backend/testing/test_dual.py"]


def test_surface_sentinel_absent_on_disk_not_emitted(tmp_path):
    """A registered sentinel that does not exist on disk is never selected."""
    root = str(tmp_path)
    _make_tree(root)
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/**/*.py",
                "sentinels": ["backend/testing/test_vanished.py"],
            }
        ],
    )
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []


def test_missing_surface_manifest_is_graceful(tmp_path):
    """No manifest file at all -> no sentinels, no crash."""
    root = str(tmp_path)
    _make_tree(root)
    # No drift_sentinel_surfaces.json written.
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []


def test_malformed_surface_manifest_is_graceful(tmp_path):
    """Invalid JSON / wrong shape degrades to no sentinels rather than raising."""
    root = str(tmp_path)
    _make_tree(root)
    _write(root, "backend/testing/test_tree_sentinel.py", "x = 1\n")
    # Invalid JSON.
    _write(root, "backend/testing/drift_sentinel_surfaces.json", "{not json")
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []
    # Valid JSON but wrong shape (surfaces not a list).
    _write(
        root,
        "backend/testing/drift_sentinel_surfaces.json",
        '{"surfaces": "nope"}',
    )
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []
    # Entry missing sentinels is skipped, not fatal.
    _write_surface_manifest(root, [{"glob": "backend/app/**/*.py"}])
    assert impacted_tests.resolve_impacted(["backend/app/foo.py"], root) == []


def test_live_surface_manifest_is_valid_and_registers_websocket_sentinel():
    """The committed manifest is well-formed and lists on-disk sentinels only.

    Uses the REAL repo root: every glob must compile, every sentinel path must
    exist under backend/testing/, and the reference websocket emitter inventory
    sentinel must be registered against the backend/app tree surface.
    """
    repo_root = impacted_tests._DEFAULT_REPO_ROOT
    manifest = impacted_tests._load_surface_manifest(repo_root)
    assert manifest, "live drift_sentinel_surfaces.json compiled to no surfaces"
    all_sentinels: set[str] = set()
    # In the Docker test harness the backend tree is copied with testing/ as a
    # sibling of app/ (no backend/ prefix), so a sentinel's repo-relative path
    # also resolves as a sibling of this test file.
    _here = os.path.dirname(os.path.abspath(__file__))
    for _regex, sentinels in manifest:
        for sentinel in sentinels:
            all_sentinels.add(sentinel)
            assert sentinel.startswith("backend/testing/"), sentinel
            sibling = os.path.join(_here, sentinel[len("backend/testing/"):])
            assert os.path.isfile(os.path.join(repo_root, sentinel)) or os.path.isfile(
                sibling
            ), f"registered sentinel missing on disk: {sentinel}"
    assert "backend/testing/test_websocket_emitter_inventory.py" in all_sentinels, (
        "reference websocket emitter inventory sentinel must be registered"
    )
    # And it must actually fire for a changed app source path.
    selected = impacted_tests.resolve_impacted(
        ["backend/app/websocket_manager.py"], repo_root
    )
    assert "backend/testing/test_websocket_emitter_inventory.py" in selected


def test_indirectly_reached_module_via_two_hop_chain(tmp_path):
    """A two-hop chain (test -> intermediate -> changed leaf) selects the test.

    Fixture: ``core.py`` (changed) <- ``service.py`` <- ``router.py``.
    The test file imports only ``app.routers.router``, not ``app.core`` or
    ``app.service``.  Changing ``core.py`` must select the test because:
      hop 1: core.py -> service.py (service imports core)
      hop 2: service.py -> routers/router.py (router imports service)
    The one-hop implementation misses hop 2 and would return [].
    """
    root = str(tmp_path)
    _make_tree(root)
    # Bottom of the chain: the module being changed.
    _write(root, "backend/app/core.py", "def logic(): pass\n")
    # Middle hop: imports core.
    _write(
        root,
        "backend/app/service.py",
        "from app.core import logic\n\ndef svc(): return logic()\n",
    )
    # Top hop: imports service only.
    _write(
        root,
        "backend/app/routers/router.py",
        "from app.service import svc\n\ndef route(): return svc()\n",
    )
    # Test file references only the router — not core or service.
    _write(
        root,
        "backend/testing/test_router.py",
        "from app.routers import router\n\ndef test_x(): assert router.route()\n",
    )
    result = impacted_tests.resolve_impacted(["backend/app/core.py"], root)
    assert "backend/testing/test_router.py" in result


def test_depth_bound_terminates_on_circular_import(tmp_path):
    """Mutual imports (a cycle) must not cause an infinite loop.

    ``mod_a`` imports ``mod_b``, ``mod_b`` imports ``mod_a``.  No test
    references either, so the result is empty, but the resolver must return
    in finite time rather than looping forever.
    """
    root = str(tmp_path)
    _make_tree(root)
    _write(root, "backend/app/mod_a.py", "from app.mod_b import b\n\ndef a(): return b()\n")
    _write(root, "backend/app/mod_b.py", "from app.mod_a import a\n\ndef b(): return None\n")
    # No test references either module — result is empty, and we must not hang.
    result = impacted_tests.resolve_impacted(["backend/app/mod_a.py"], root)
    assert result == []


def test_resolver_reads_each_app_and_test_source_once(tmp_path, monkeypatch):
    """Transitive expansion is indexed; hop count never multiplies file scans."""
    root = str(tmp_path)
    _make_tree(root)
    _write(root, "backend/app/leaf.py", "def leaf(): pass\n")
    previous = "leaf"
    for hop in range(impacted_tests.MAX_HOPS):
        current = f"layer_{hop}"
        _write(
            root,
            f"backend/app/{current}.py",
            f"from app.{previous} import value\n",
        )
        previous = current
    for index in range(5):
        _write(
            root,
            f"backend/testing/test_scan_{index}.py",
            f"from app.{previous} import value\n",
        )

    opened_py: "dict[str, int]" = {}
    real_open = open

    def counting_open(path, *args, **kwargs):
        normalized = os.path.normpath(os.fspath(path))
        if normalized.endswith(".py"):
            opened_py[normalized] = opened_py.get(normalized, 0) + 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    impacted_tests.resolve_impacted(["backend/app/leaf.py"], root)

    app_and_test_sources = [
        path
        for path in opened_py
        if os.sep + "backend" + os.sep + "app" + os.sep in path
        or os.sep + "backend" + os.sep + "testing" + os.sep in path
    ]
    assert app_and_test_sources
    assert all(opened_py[path] == 1 for path in app_and_test_sources), opened_py


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
    # First 2 (most relevant) emitted; the 3rd deferred as unverified on stderr.
    assert stdout == [
        "backend/testing/test_mod0.py",
        "backend/testing/test_mod1.py",
    ]
    assert stderr == ["deferred-unverified: backend/testing/test_mod2.py"]


def test_directly_changed_tests_rank_first(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # A PR that both adds its own test and changes an app module: the direct
    # test must run before any reference-matched file, regardless of alphabet.
    _write(root, "backend/testing/test_zzz_direct.py", "def test_new(): assert True\n")
    _write(
        root,
        "backend/testing/test_aaa_user.py",
        "from app.foo import bar\n",
    )
    result = impacted_tests.resolve_impacted(
        ["backend/app/foo.py", "backend/testing/test_zzz_direct.py"], root
    )
    assert result == [
        "backend/testing/test_zzz_direct.py",
        "backend/testing/test_aaa_user.py",
    ]


def test_hop_zero_ranks_before_hop_one(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # service.py imports the changed core.py; a test that references the changed
    # module directly (hop 0) ranks above one that reaches it only via the
    # importer (hop 1) — the direct contract is what the change exercises.
    _write(root, "backend/app/core.py", "def logic(): pass\n")
    _write(
        root,
        "backend/app/service.py",
        "from app.core import logic\n\ndef svc(): return logic()\n",
    )
    _write(
        root,
        "backend/testing/test_core_user.py",
        "from app.core import logic\n",
    )
    _write(
        root,
        "backend/testing/test_via_service.py",
        "from app.service import svc\n",
    )
    result = impacted_tests.resolve_impacted(["backend/app/core.py"], root)
    assert result == [
        "backend/testing/test_core_user.py",
        "backend/testing/test_via_service.py",
    ]


def test_mock_patch_coupling_ranks_within_hop(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # Two tests both reference the changed module (hop 0, same reference
    # breadth); the one that string-patches its internals is more coupled to the
    # change — a behavior/signature drift breaks the patch — so it ranks first.
    _write(
        root,
        "backend/testing/test_foo_patcher.py",
        "from unittest.mock import patch\n\n\n"
        "def test_x():\n"
        '    with patch("app.foo.bar", return_value=9):\n'
        "        assert True\n",
    )
    _write(
        root,
        "backend/testing/test_foo_importer.py",
        "from app.foo import bar\n",
    )
    result = impacted_tests.resolve_impacted(["backend/app/foo.py"], root)
    assert result == [
        "backend/testing/test_foo_patcher.py",
        "backend/testing/test_foo_importer.py",
    ]


def test_broader_direct_reference_ranks_within_hop(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # Same hop, no patch coupling: the test that references BOTH changed modules
    # directly (n_direct=2) ranks above the one referencing a single module.
    _write(root, "backend/app/other.py", "def q(): pass\n")
    _write(
        root,
        "backend/testing/test_both_direct.py",
        "from app.foo import bar\nfrom app.other import q\n",
    )
    _write(
        root,
        "backend/testing/test_foo_only.py",
        "from app.foo import bar\n",
    )
    result = impacted_tests.resolve_impacted(
        ["backend/app/foo.py", "backend/app/other.py"], root
    )
    assert result == [
        "backend/testing/test_both_direct.py",
        "backend/testing/test_foo_only.py",
    ]


def test_drift_sentinel_ranks_last(tmp_path):
    root = str(tmp_path)
    _make_tree(root)
    # A reference-matched test (hop 0) must outrank a surface-glob drift sentinel
    # selected for the same change — the sentinel has no import path and is only
    # a whole-tree guard, not a direct contract test.
    _write(
        root,
        "backend/testing/test_foo_user.py",
        "from app.foo import bar\n",
    )
    _write(
        root,
        "backend/testing/test_tree_sentinel.py",
        "from websocket_event_inventory import build\n",
    )
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/**/*.py",
                "sentinels": ["backend/testing/test_tree_sentinel.py"],
            }
        ],
    )
    result = impacted_tests.resolve_impacted(["backend/app/foo.py"], root)
    assert result == [
        "backend/testing/test_foo_user.py",
        "backend/testing/test_tree_sentinel.py",
    ]


def test_live_redrive_change_ranks_finalize_qualification_test_in_top_cap(tmp_path):
    """Regression for promotion PR #16761: relevance ranking surfaces the stale test.

    The merge gate caps the impacted-test run at 8 files. Alphabetically,
    ``test_release_qualifier__finalize_qualification_2.py`` sat at ~rank 331 of
    the 464-file impacted set for a change to
    ``backend/app/release_qualifier_redrive.py``, so the cap silently dropped it
    and the stale test merged green. Relevance ranking must place it inside the
    cap.

    The real tree is read through a ``backend/`` symlink so the assertion runs
    identically on a dev checkout and inside the Docker harness (where the
    backend tree is bind-mounted at /app with app/ and testing/ as siblings of
    the repo root, and the resolver's repo-relative paths do not resolve).
    """
    root = impacted_tests._DEFAULT_REPO_ROOT
    real_backend = os.path.join(root, "backend")
    if not os.path.isdir(real_backend):
        # Docker harness: _DEFAULT_REPO_ROOT resolves to /; the tree is /app.
        candidate = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(impacted_tests.__file__)), "..")
        )
        if os.path.isdir(os.path.join(candidate, "app")) and os.path.isdir(
            os.path.join(candidate, "testing")
        ):
            real_backend = candidate
        else:
            pytest.skip("backend tree not found in this layout")
    fake_root = os.path.join(str(tmp_path), "repo")
    os.makedirs(fake_root, exist_ok=True)
    os.symlink(real_backend, os.path.join(fake_root, "backend"), target_is_directory=True)
    target = "backend/testing/test_release_qualifier__finalize_qualification_2.py"
    ranked = impacted_tests.resolve_impacted(
        ["backend/app/release_qualifier_redrive.py"], fake_root
    )
    assert target in ranked
    assert ranked.index(target) < 8
