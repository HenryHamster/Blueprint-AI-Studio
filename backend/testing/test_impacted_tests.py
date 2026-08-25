"""Tests for the reverse-dependency resolver in ``impacted_tests.py``.

The resolver powers the merge-time impacted-test gate: it maps a set of changed
repo-relative paths to the ``backend/testing/test_*.py`` files that exercise the
changed modules (by import or by string mock-patch target), plus any directly
changed/added test file. These tests build a hermetic fixture tree under
``tmp_path`` and assert the mapping, boundary handling, and edge cases.
"""

import io
import json
import os

import pytest

from testing import impacted_tests
from testing.fixgate_attribution import format_offender_marker


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
            # A sentinel may be registered as a pytest node id
            # (``file.py::node``) when the rest of its module needs a database;
            # the on-disk assertion applies to the FILE part, exactly as
            # impacted_tests._sentinel_exists does.
            rel = impacted_tests.sentinel_file(sentinel)
            sibling = os.path.join(_here, rel[len("backend/testing/"):])
            assert os.path.isfile(os.path.join(repo_root, rel)) or os.path.isfile(
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


def test_load_surface_manifest_is_thin_wrapper_over_private_loader(tmp_path):
    """The public accessor returns exactly what the private loader compiles.

    ``scripts/static_release_guard.py`` (outside backend/testing) consumes the
    manifest via this public wrapper rather than reaching into the
    underscore-prefixed ``_load_surface_manifest``; the two must stay in lockstep.
    """
    root = str(tmp_path)
    _make_tree(root)
    _write_surface_manifest(
        root,
        [
            {
                "glob": "admin-ui/**",
                "sentinels": ["backend/testing/test_tree_sentinel.py"],
            }
        ],
    )
    _write(root, "backend/testing/test_tree_sentinel.py", "x = 1\n")
    private = impacted_tests._load_surface_manifest(root)
    public = impacted_tests.load_surface_manifest(root)
    assert [p.pattern for p, _ in public] == [p.pattern for p, _ in private]
    assert [s for _, s in public] == [s for _, s in private]
    assert public, "wrapper compiled no surfaces from a valid manifest"


def test_admin_ui_surface_change_resolves_the_vitest_durations_sentinel():
    """A changed admin-ui/ path selects the vitest durations freshness sentinel.

    This is the live-manifest regression for the gap this feature closes:
    ``test_vitest_durations_freshness.py`` reads admin-ui/.test_durations.json
    and rglobs admin-ui/src, but imports no backend/app module and lives outside
    STATIC_PATH_PREFIXES, so an admin-ui-only PR previously selected neither the
    reverse-dep scan nor the static release guard's scope gate for it.
    """
    repo_root = impacted_tests._DEFAULT_REPO_ROOT
    selected = impacted_tests.resolve_impacted(
        ["admin-ui/src/components/MessagesPanel.tsx"], repo_root
    )
    assert "backend/testing/test_vitest_durations_freshness.py" in selected


def test_agent_surface_change_resolves_the_vitest_durations_sentinel():
    """Same coverage as the admin-ui case above, for the agent/ Vitest suite."""
    repo_root = impacted_tests._DEFAULT_REPO_ROOT
    selected = impacted_tests.resolve_impacted(
        ["agent/src/handlers/strategic.ts"], repo_root
    )
    assert "backend/testing/test_vitest_durations_freshness.py" in selected


def test_admin_ui_src_change_resolves_conversational_workday_sentinel():
    """A changed admin-ui/src/ path selects the conversational Workday guard.

    ``conversational_workday_manifest.json``'s PROTECT list names files under
    admin-ui/src, agent/src, and scripts/ — none of which fall under
    STATIC_PATH_PREFIXES or the dotted-import reverse-dep scan.
    """
    repo_root = impacted_tests._DEFAULT_REPO_ROOT
    selected = impacted_tests.resolve_impacted(
        ["admin-ui/src/components/MessagesPanel.tsx"], repo_root
    )
    assert "backend/testing/test_conversational_workday_protection.py" in selected


def test_scripts_change_resolves_conversational_workday_sentinel():
    """A changed protected scripts/ file also selects the conversational Workday guard."""
    repo_root = impacted_tests._DEFAULT_REPO_ROOT
    selected = impacted_tests.resolve_impacted(
        ["scripts/test_runner_isolation.py"], repo_root
    )
    assert "backend/testing/test_conversational_workday_protection.py" in selected


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

    opened_py: dict[str, int] = {}
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
    assert all(opened_py[path] == 1 for path in app_and_test_sources), (
        str(opened_py)
        + "\n"
        + format_offender_marker(sorted(app_and_test_sources))
    )


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
    assert stderr == [
        "deferred-rank: backend/testing/test_mod2.py",
        "rank-cap: 1 impacted file(s) beyond --limit 2 deferred",
    ]


def test_capped_impacted_selection_retains_surface_sentinels(tmp_path, capsys):
    """The ordinary cap never drops a matched sentinel from the gate payload."""
    root = str(tmp_path)
    _make_tree(root)
    for index in range(12):
        _write(
            root,
            f"backend/testing/test_fanout_{index:02d}.py",
            "from app.foo import bar\n",
        )
    for name in ("test_tree_sentinel.py", "test_event_sentinel.py"):
        _write(root, f"backend/testing/{name}", "def test_inventory(): pass\n")
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/**/*.py",
                "sentinels": [
                    "backend/testing/test_tree_sentinel.py",
                    "backend/testing/test_event_sentinel.py",
                    "backend/testing/test_tree_sentinel.py",
                ],
            }
        ],
    )

    ordinary, mandatory = impacted_tests.resolve_impacted_partition(
        ["backend/app/foo.py"], root
    )
    assert len(ordinary) == 12
    assert mandatory == [
        "backend/testing/test_event_sentinel.py",
        "backend/testing/test_tree_sentinel.py",
    ]

    mandatory_output = os.path.join(root, "mandatory.txt")
    rc = impacted_tests.main(
        [
            "backend/app/foo.py",
            "--limit",
            "8",
            "--mandatory-output",
            mandatory_output,
            "--repo-root",
            root,
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ordinary[:8]
    assert captured.err.splitlines() == [
        f"deferred-rank: {path}" for path in ordinary[8:]
    ] + [f"rank-cap: {len(ordinary) - 8} impacted file(s) beyond --limit 8 deferred"]
    with open(mandatory_output, encoding="utf-8") as fh:
        assert fh.read().splitlines() == mandatory


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


def _repo_root_with(relative: str) -> "str | None":
    """First checkout root that actually holds *relative*, else None.

    Mirrors the resolution the sibling estate guards use: the Docker harness
    bind-mounts the repo read-only at /repo while the backend tree is mounted
    separately, so ``_DEFAULT_REPO_ROOT`` alone does not find repo-level files.
    """
    here = os.path.dirname(os.path.abspath(impacted_tests.__file__))
    candidates = [
        os.environ.get("BLUEPRINT_REPO_ROOT", ""),
        "/repo",
        impacted_tests._DEFAULT_REPO_ROOT,
        os.path.normpath(os.path.join(here, "..", "..")),
    ]
    for root in candidates:
        if root and os.path.isfile(os.path.join(root, relative)):
            return root
    return None


def _standalone_roster(harness_text: str) -> "set[str]":
    """Backend selectors in scripts/test-backend.sh's literal STANDALONE_ARGS.

    Parsed from the harness source rather than by invoking
    ``--standalone-inventory``: this assertion has to hold in the --noconftest,
    Docker-less standalone lane, which cannot start the harness it is checking.
    Only the literal ``testing/...`` entries are collected — the scripts/ and
    infra/ rosters are glob-discovered and hold no drift sentinel.
    """
    marker = "STANDALONE_ARGS=("
    start = harness_text.index(marker) + len(marker)
    end = harness_text.index("\n    )", start)
    return {
        line.strip()
        for line in harness_text[start:end].splitlines()
        if line.strip().startswith("testing/")
    }


def _glob_probe(glob: str) -> str:
    """A concrete path that the surface glob matches, for reachability tests."""
    return glob.replace("**/", "probe/").replace("**", "probe").replace("*", "probe")


def test_every_registered_sentinel_is_reachable_from_its_own_surface():
    """A sentinel must actually RUN for a change that matches its surface glob.

    Two lanes can run a registered sentinel:

    * the backend-authoring lane, which resolves selectors only when the diff
      contains a changed ``backend/*.py`` file; and
    * the always-on attributed standalone corpus, whose roster is the fixed
      ``STANDALONE_ARGS`` list in scripts/test-backend.sh.

    So a sentinel registered for a surface that CANNOT contain a backend Python
    file (admin-ui/, agent/, scripts/, a JSON/markdown artifact) is dead
    registration unless the standalone roster carries it: nothing selects it,
    and the drift it guards merges green until the promotion matrix — which has
    no base to attribute against — surfaces every accumulated guard at once.
    That is what happened on promotion PR #17738.
    """
    root = _repo_root_with("scripts/test-backend.sh")
    if root is None:
        pytest.skip("scripts/test-backend.sh not available in this layout")
    with open(os.path.join(root, "scripts", "test-backend.sh"), encoding="utf-8") as fh:
        roster = _standalone_roster(fh.read())
    # Literal pin: a roster that parsed to nothing must not silently pass.
    assert "testing/test_impacted_tests.py" in roster
    manifest_path = os.path.join(
        root, "backend", "testing", "drift_sentinel_surfaces.json"
    )
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)["surfaces"]
    unreachable: "list[str]" = []
    for entry in manifest:
        glob = entry["glob"]
        probe = _glob_probe(glob)
        if probe.startswith("backend/") and probe.endswith(".py"):
            # The backend-authoring lane's resolver can select these.
            continue
        for sentinel in entry["sentinels"]:
            harness_form = sentinel.split("backend/", 1)[-1]
            if harness_form in roster:
                continue
            if impacted_tests.sentinel_file(harness_form) in roster:
                continue
            unreachable.append(f"{glob} -> {sentinel}")
    assert not unreachable, (
        "drift sentinels registered for a non-backend-Python surface but absent "
        "from scripts/test-backend.sh STANDALONE_ARGS, so nothing runs them: "
        + format_offender_marker(sorted(unreachable))
    )


def test_committed_manifest_registers_the_guards_that_failed_promotion_17738():
    """Literal pin on the four registrations added after PR #17738.

    Each of these guards protects a DERIVED artifact and was previously
    unregistered, so the stale artifact reached the promotion matrix.
    """
    root = _repo_root_with("backend/testing/drift_sentinel_surfaces.json")
    if root is None:
        pytest.skip("surface manifest not available in this layout")
    cases = {
        "backend/app/recovery_evaluating_prless_hold.py": (
            "backend/testing/test_operator_workbench_contracts.py"
            "::test_websocket_emitter_inventory_covers_current_emitters"
        ),
        "backend/.test_durations": (
            "backend/testing/test_test_durations_freshness.py"
        ),
        "test-estate-ranked-queue.md": (
            "backend/testing/test_test_estate_ranked_queue.py"
        ),
        "backend/testing/conversational_workday_manifest.json": (
            "backend/testing/test_conversational_workday_protection.py"
        ),
    }
    for changed, sentinel in cases.items():
        selected = impacted_tests.surface_sentinels([changed], root)
        assert sentinel in selected, (
            f"{changed} no longer selects {sentinel}; "
            + format_offender_marker(sorted(selected))
        )


def test_node_id_sentinel_selects_and_reports_its_node(tmp_path):
    """A ``file.py::node`` registration selects that exact node, not the file.

    The node-id form exists for a guard whose sibling tests need a database:
    registering the whole file would drag DB-dependent tests into the
    --no-postgres standalone route, where they ERROR on a missing fixture
    instead of reporting drift.
    """
    root = str(tmp_path)
    _make_tree(root)
    _write(root, "backend/testing/test_guard.py", "def test_a():\n    pass\n")
    _write(
        root,
        "backend/testing/drift_sentinel_surfaces.json",
        '{"surfaces": [{"glob": "admin-ui/**", "sentinels": '
        '["backend/testing/test_guard.py::test_a"]}]}',
    )
    ordinary, mandatory = impacted_tests.resolve_impacted_partition(
        ["admin-ui/src/App.tsx"], root
    )
    assert mandatory == ["backend/testing/test_guard.py::test_a"]
    assert ordinary == []


def test_node_id_sentinel_existence_check_uses_the_file_part(tmp_path):
    """A node id whose FILE is gone must not select — same discipline as a file."""
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/drift_sentinel_surfaces.json",
        '{"surfaces": [{"glob": "admin-ui/**", "sentinels": '
        '["backend/testing/test_vanished.py::test_a"]}]}',
    )
    assert impacted_tests.surface_sentinels(["admin-ui/src/App.tsx"], root) == set()


def test_node_id_sentinel_collapses_into_a_whole_file_selection(tmp_path):
    """When the file is already selected, the node id must not be emitted too.

    Emitting both would run the module twice in a single gate invocation, and
    would also let the collapse demote the sentinel out of the mandatory
    partition into the capped ordinary budget.
    """
    root = str(tmp_path)
    _make_tree(root)
    _write(
        root,
        "backend/testing/test_guard.py",
        "from app.foo import bar\n\n\ndef test_a():\n    assert bar() == 1\n",
    )
    _write(
        root,
        "backend/testing/drift_sentinel_surfaces.json",
        '{"surfaces": [{"glob": "backend/app/**/*.py", "sentinels": '
        '["backend/testing/test_guard.py::test_a"]}]}',
    )
    ordinary, mandatory = impacted_tests.resolve_impacted_partition(
        ["backend/app/foo.py"], root
    )
    assert mandatory == ["backend/testing/test_guard.py"]
    assert ordinary == []


# ---------------------------------------------------------------------------
# create=True prefilter soundness
# ---------------------------------------------------------------------------


def test_create_true_prefilter_admits_every_spelling_the_ast_scan_accepts(tmp_path):
    """The textual prefilter must never reject a file the AST scan would flag.

    ``collect_create_true_patch_targets`` skips ``ast.parse`` for any file whose
    text does not match ``_CREATE_TRUE_HINT``. That is a 14.8x saving and a
    silent-vacuity risk in the same edit: a prefilter that is too narrow makes
    the redundant-create=True invariant stop seeing real offenders, and nothing
    fails to say so.

    The soundness argument is that between two adjacent Python tokens the
    grammar admits only whitespace, a backslash line-continuation, and comments
    -- so ``create=True`` cannot be spelled in a way that defeats a pattern
    which allows exactly those three. This pins the argument against the
    scanner: every spelling below must be BOTH admitted by the prefilter AND
    reported by the collector, so the two can never drift apart.
    """
    spellings = {
        "plain": 'mocker.patch("app.m.a", create=True)',
        "spaces_around_equals": 'mocker.patch("app.m.a", create = True)',
        "newline_before_value": 'mocker.patch("app.m.a", create=\n        True)',
        "newline_after_name": 'mocker.patch("app.m.a", create\n        =True)',
        "comment_after_equals": 'mocker.patch("app.m.a", create=  # why\n        True)',
        "comment_before_equals": 'mocker.patch("app.m.a", create  # c\n        =True)',
        "line_continuation": 'mocker.patch("app.m.a", create=\\\n        True)',
        "tab_separated": 'mocker.patch("app.m.a", create\t=\tTrue)',
    }

    for label, call in spellings.items():
        source = f"def test_x(mocker):\n    {call}\n"

        assert impacted_tests._CREATE_TRUE_HINT.search(source), (
            f"prefilter REJECTS the {label!r} spelling of create=True. The "
            "collector would skip the parse and the redundant-create=True "
            "invariant would go silently vacuous for files written that way."
        )

        target_dir = tmp_path / label
        target_dir.mkdir()
        (target_dir / "test_probe.py").write_text(source, encoding="utf-8")
        pairs = impacted_tests.collect_create_true_patch_targets(str(target_dir))
        assert pairs == [("test_probe.py", "app.m.a")], (
            f"collector no longer reports the {label!r} spelling; the prefilter "
            "assertion above is measuring nothing."
        )


def test_create_true_prefilter_skips_files_with_nothing_to_find(tmp_path):
    """A file the prefilter rejects must genuinely have no pair to report.

    The other half of the pin: prefilter and scanner must agree on the negative
    case too, or the saving is coming from somewhere it should not.
    """
    source = (
        "def test_x(mocker):\n"
        '    mocker.patch("app.m.a")\n'
        '    mocker.patch("app.m.b", autospec=True)\n'
        "    create_something(True)\n"
    )
    (tmp_path / "test_probe.py").write_text(source, encoding="utf-8")

    assert not impacted_tests._CREATE_TRUE_HINT.search(source)
    assert impacted_tests.collect_create_true_patch_targets(str(tmp_path)) == []


def test_create_true_prefilter_does_not_backtrack_on_adversarial_input():
    """The token-gap group is atomic, so a long non-match cannot blow up.

    ``(?>[\\s\\\\]|\\#[^\\n]*)*`` after a literal ``create`` would be a classic
    catastrophic-backtracking shape if the group were not atomic: a file full of
    ``create`` followed by megabytes of whitespace that never reaches ``=``.
    A regression to a non-atomic group would hang the whole standalone lane
    rather than merely slow it, so this pins the property directly.
    """
    import time

    adversarial = "create" + (" \n\t" * 12000) + "x"
    start = time.perf_counter()
    assert impacted_tests._CREATE_TRUE_HINT.search(adversarial) is None
    # round() rather than a bare subtraction: the standalone offender-identity
    # guard reads any ``x = a - b`` as a set difference, so a plain elapsed
    # time would be mistaken for an offender collection owing a marker.
    elapsed_seconds = round(time.perf_counter() - start, 3)
    assert elapsed_seconds < 1.0, (
        f"prefilter took {elapsed_seconds}s on a 40KB non-matching input; the "
        "token gap has lost its atomic group and is backtracking."
    )


# ---------------------------------------------------------------------------
# Cost budgeting.
#
# A selector COUNT cap cannot bound this gate's wall time, and that is measured
# rather than assumed: costed against the committed backend/.test_durations,
# the eight heaviest backend test files sum to ~42.6 minutes, so an eight-file
# selection can outlast any timeout it is given and be cascade-killed while
# perfectly healthy -- the harness-budget-inversion class. These cover the
# duration budget that actually bounds it.
# ---------------------------------------------------------------------------


def _write_durations(root, mapping):
    path = os.path.join(root, "durations.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh)
    return path


def test_load_test_file_costs_sums_node_ids_into_repo_relative_files(tmp_path):
    path = _write_durations(
        str(tmp_path),
        {
            "testing/test_a.py::test_one": 1.5,
            "testing/test_a.py::test_two": 2.5,
            "testing/test_b.py::test_only": 4.0,
        },
    )
    costs = impacted_tests.load_test_file_costs(path)
    # Per-node measurements roll up per FILE, because selection is per file.
    assert costs == {
        "backend/testing/test_a.py": 4.0,
        "backend/testing/test_b.py": 4.0,
    }


def test_load_test_file_costs_is_fail_open_on_unusable_input(tmp_path):
    missing = os.path.join(str(tmp_path), "nope.json")
    assert impacted_tests.load_test_file_costs(missing) == {}

    not_a_mapping = os.path.join(str(tmp_path), "list.json")
    with open(not_a_mapping, "w", encoding="utf-8") as fh:
        json.dump(["testing/test_a.py::test_one"], fh)
    assert impacted_tests.load_test_file_costs(not_a_mapping) == {}

    malformed = os.path.join(str(tmp_path), "bad.json")
    with open(malformed, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert impacted_tests.load_test_file_costs(malformed) == {}

    # A single unusable VALUE is skipped without discarding the usable rows.
    mixed = _write_durations(
        str(tmp_path),
        {"testing/test_a.py::test_one": "slow", "testing/test_b.py::test_x": 3.0},
    )
    assert impacted_tests.load_test_file_costs(mixed) == {
        "backend/testing/test_b.py": 3.0
    }


def test_apply_cost_budget_skips_rather_than_stops_at_an_oversized_entry():
    ordinary = ["a", "big", "b", "c"]
    costs = {"a": 10.0, "big": 500.0, "b": 10.0, "c": 10.0}
    kept, deferred, spent = impacted_tests.apply_cost_budget(ordinary, costs, 100.0)
    # Walking must CONTINUE past an entry that does not fit: one expensive
    # high-rank file must not starve every cheaper file ranked behind it.
    assert kept == ["a", "b", "c"]
    assert deferred == ["big"]
    assert spent == 30.0


def test_apply_cost_budget_keeps_the_top_entry_even_when_it_alone_overruns():
    kept, deferred, spent = impacted_tests.apply_cost_budget(
        ["heaviest", "cheap"], {"heaviest": 900.0, "cheap": 1.0}, 600.0
    )
    # Selecting NOTHING is the one outcome a merge gate must never produce, so
    # the most relevant entry is exempt. This is why the honest bound is
    # `budget + cost of the single heaviest selected file`, not `budget`.
    assert kept == ["heaviest"]
    assert deferred == ["cheap"]
    assert spent == 900.0


def test_apply_cost_budget_charges_mandatory_sentinels_against_the_budget():
    ordinary = ["a", "b"]
    costs = {"a": 60.0, "b": 60.0}
    unreserved, _, _ = impacted_tests.apply_cost_budget(ordinary, costs, 100.0)
    assert unreserved == ["a"]
    kept, deferred, _ = impacted_tests.apply_cost_budget(
        ordinary, costs, 100.0, reserved_seconds=90.0
    )
    # Mandatory sentinels are never dropped, but they do burn wall clock; a
    # budget that ignored them would understate the gate's real runtime.
    assert kept == ["a"]
    assert deferred == ["b"]


def test_apply_cost_budget_declines_when_it_cannot_measure():
    ordinary = ["a", "b", "c"]
    # No measurements, and a zero/absent budget: fail OPEN in both cases.
    # Dropping coverage because a data file was missing would be the vacuous
    # -guard shape this module exists to avoid.
    assert impacted_tests.apply_cost_budget(ordinary, {}, 1.0) == (ordinary, [], 0.0)
    assert impacted_tests.apply_cost_budget(ordinary, {"a": 1.0}, 0) == (
        ordinary,
        [],
        0.0,
    )
    assert impacted_tests.apply_cost_budget(ordinary, {"a": 1.0}, None) == (
        ordinary,
        [],
        0.0,
    )


def test_main_budget_defers_under_a_distinct_token_from_the_rank_cap(tmp_path, capsys):
    root = str(tmp_path)
    _make_tree(root)
    for index in range(4):
        _write(
            root,
            f"backend/testing/test_mod{index}.py",
            "from app.foo import bar\n",
        )
    durations = _write_durations(
        root,
        {
            "testing/test_mod0.py::t": 10.0,
            "testing/test_mod1.py::t": 10.0,
            "testing/test_mod2.py::t": 500.0,
            "testing/test_mod3.py::t": 10.0,
        },
    )
    rc = impacted_tests.main(
        [
            "backend/app/foo.py",
            "--limit",
            "3",
            "--budget-seconds",
            "100",
            "--durations",
            durations,
            "--repo-root",
            root,
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "backend/testing/test_mod0.py",
        "backend/testing/test_mod1.py",
    ]
    # The two deferral classes carry DIFFERENT tokens on purpose. A rank
    # deferral is the long tail the protocol deliberately declines to run (for
    # a core module that is hundreds of files); a budget deferral was ranked in
    # as relevant and dropped only for wall clock. Reported alike, the handful
    # that deserve attention are buried under the tail that never did.
    assert captured.err.splitlines() == [
        "deferred-rank: backend/testing/test_mod3.py",
        "rank-cap: 1 impacted file(s) beyond --limit 3 deferred",
        "deferred-budget: backend/testing/test_mod2.py",
        "budget: 20s of 100s (2 ordinary + 0 mandatory, 1 deferred)",
    ]


def test_main_budget_says_so_when_it_could_not_measure(tmp_path, capsys):
    root = str(tmp_path)
    _make_tree(root)
    _write(root, "backend/testing/test_mod0.py", "from app.foo import bar\n")
    absent = os.path.join(root, "absent.json")
    rc = impacted_tests.main(
        [
            "backend/app/foo.py",
            "--budget-seconds",
            "100",
            "--durations",
            absent,
            "--repo-root",
            root,
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["backend/testing/test_mod0.py"]
    # "Budgeting was requested and did not happen" and "budgeting happened and
    # dropped nothing" are identical on STDOUT. A caller that sized its timeout
    # around the budget has to be able to tell them apart, so the fail-open
    # path announces that it fell open.
    assert captured.err.splitlines() == [
        f"budget: no usable measurements in {absent} — cost budget NOT applied"
    ]


def test_main_budget_charges_mandatory_sentinels_and_never_defers_them(
    tmp_path, capsys
):
    root = str(tmp_path)
    _make_tree(root)
    for index in range(3):
        _write(
            root,
            f"backend/testing/test_mod{index}.py",
            "from app.foo import bar\n",
        )
    _write(root, "backend/testing/test_tree_sentinel.py", "def test_i(): pass\n")
    _write_surface_manifest(
        root,
        [
            {
                "glob": "backend/app/**/*.py",
                "sentinels": ["backend/testing/test_tree_sentinel.py"],
            }
        ],
    )
    durations = _write_durations(
        root,
        {
            "testing/test_tree_sentinel.py::t": 80.0,
            "testing/test_mod0.py::t": 10.0,
            "testing/test_mod1.py::t": 30.0,
            "testing/test_mod2.py::t": 30.0,
        },
    )
    mandatory_output = os.path.join(root, "mandatory.txt")
    rc = impacted_tests.main(
        [
            "backend/app/foo.py",
            "--limit",
            "8",
            "--budget-seconds",
            "100",
            "--durations",
            durations,
            "--mandatory-output",
            mandatory_output,
            "--repo-root",
            root,
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    # 80s of the 100s budget is spent before an ordinary selector is
    # considered, so only the 10s file fits -- but the sentinel itself is
    # carried regardless, because surface drift is the contract this gate
    # cannot trade away for speed.
    assert captured.out.splitlines() == ["backend/testing/test_mod0.py"]
    with open(mandatory_output, encoding="utf-8") as fh:
        assert fh.read().splitlines() == ["backend/testing/test_tree_sentinel.py"]
    err = captured.err.splitlines()
    assert "deferred-budget: backend/testing/test_mod1.py" in err
    assert "deferred-budget: backend/testing/test_mod2.py" in err
    assert not any(
        line.startswith("deferred") and line.endswith("test_tree_sentinel.py")
        for line in err
    )
    assert err[-1] == "budget: 90s of 100s (1 ordinary + 1 mandatory, 2 deferred)"
