"""Regression coverage for running static release guards on the task diff."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_static_guard_base_unpack_uses_data_filter() -> None:
    """The internal git-archive unpack still uses an explicit safe filter."""
    source = (REPO_ROOT / "scripts" / "static_release_guard.py").read_text()
    assert 'bundle.extractall(destination, filter="data")' in source


def test_check_runs_static_guard_only_for_test_gate() -> None:
    """The release guard is invoked only by the explicit test/merge gate."""
    source = (REPO_ROOT / "check").read_text()
    release_block_start = source.index("# The database-free release guard corpus")
    release_block_end = source.index("# Idempotent npm install", release_block_start)
    release_block = source[release_block_start:release_block_end]
    guard_call = 'run "static release guards (task diff)" python3 scripts/static_release_guard.py'
    assert 'if [ "$WITH_TESTS" = "1" ]; then' in release_block
    assert guard_call in release_block
    assert "--size-only" not in release_block
    assert "static release guard deferred" in release_block
    assert "refusing to skip the gate" in release_block


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_task_diff_breaching_size_guard_fails_check(tmp_path: Path) -> None:
    """A changed cap that makes a source file over-limit must fail the gate."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "testing").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "check", tmp_path / "check")
    shutil.copy2(
        REPO_ROOT / "scripts" / "static_release_guard.py",
        tmp_path / "scripts" / "static_release_guard.py",
    )
    shutil.copy2(
        REPO_ROOT / "scripts" / "backend-test-route.sh",
        tmp_path / "scripts" / "backend-test-route.sh",
    )
    shutil.copy2(
        REPO_ROOT / "backend" / "testing" / "check_size_limits.py",
        tmp_path / "backend" / "testing" / "check_size_limits.py",
    )
    shutil.copy2(
        REPO_ROOT / "backend" / "app" / "size_limit_policy.py",
        tmp_path / "backend" / "app" / "size_limit_policy.py",
    )
    os.chmod(tmp_path / "check", 0o755)
    os.chmod(tmp_path / "scripts" / "static_release_guard.py", 0o755)
    os.chmod(tmp_path / "scripts" / "backend-test-route.sh", 0o755)
    (tmp_path / "backend" / "app" / "size_probe.py").write_text("line_one = 1\nline_two = 2\n")

    limits = {"backend": {"size_probe.py": 2}, "agent": {}, "frontend": {}}
    limits_path = tmp_path / "backend" / "testing" / "module_size_limits.json"
    limits_path.write_text(json.dumps(limits) + "\n")

    _git(tmp_path, "init", "-b", "automain")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "switch", "-c", "task")

    limits["backend"]["size_probe.py"] = 1
    limits_path.write_text(json.dumps(limits) + "\n")
    _git(tmp_path, "add", str(limits_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "breach size cap")

    result = subprocess.run(
        ["bash", str(tmp_path / "check"), "test"],
        cwd=tmp_path,
        env={**os.environ, "WORKSPACE": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, (
        "the current per-task gate incorrectly passed a changed diff whose "
        "module_size_limits.json cap makes size_probe.py exceed its limit\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    output = result.stdout + result.stderr
    assert "module size" in output.lower()
    assert "size_probe.py" in output


def _prepare_docker_attribution_repo(
    tmp_path: Path, current_state: str, base_state: str = "preexisting"
) -> tuple[Path, Path]:
    """Build a tiny repository that exercises the real Docker harness path."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "backend" / "testing").mkdir(parents=True)
    (tmp_path / "backend" / "app").mkdir(parents=True)
    for relative in (
        "static_release_guard.py",
        "test-backend.sh",
        "backend-test-route.sh",
    ):
        source = REPO_ROOT / "scripts" / relative
        shutil.copy2(source, tmp_path / "scripts" / relative)
    shutil.copy2(
        REPO_ROOT / "backend" / "testing" / "check_size_limits.py",
        tmp_path / "backend" / "testing" / "check_size_limits.py",
    )
    (tmp_path / "backend" / "requirements.txt").write_text("pytest\n")
    (tmp_path / "backend" / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "backend" / "testing" / "test_existing.py").write_text(
        "def test_preexisting():\n    raise AssertionError('fixture only')\n"
    )
    # A pre-existing failure that lives under scripts/ rather than backend/. In
    # Docker mode the harness selects it as /repo/scripts/... from a pytest
    # rootdir of /app, so its JUnit classname keeps the "repo" mount segment —
    # the shape classname->path resolution has to handle or the failure is
    # silently misattributed as new.
    (tmp_path / "scripts" / "test_preexisting_roster.py").write_text(
        "def test_roster_preexisting():\n    raise AssertionError('fixture only')\n"
    )
    # A module that fails at COLLECTION time in both trees. pytest reports that
    # with an EMPTY classname and the dotted module path in `name`, which is the
    # shape a classname-only resolver cannot translate into a selector.
    (tmp_path / "backend" / "testing" / "test_collect_boom.py").write_text(
        "raise ImportError('fixture only')\n"
    )
    shutil.copy2(
        REPO_ROOT / "backend" / "app" / "size_limit_policy.py",
        tmp_path / "backend" / "app" / "size_limit_policy.py",
    )
    (tmp_path / "backend" / "testing" / "module_size_limits.json").write_text(
        '{"backend": {}, "agent": {}, "frontend": {}}\n'
    )
    (tmp_path / "attribution_state").write_text(f"{base_state}\n")
    for relative in (
        "scripts/static_release_guard.py",
        "scripts/test-backend.sh",
        "scripts/backend-test-route.sh",
    ):
        os.chmod(tmp_path / relative, 0o755)

    docker_bin = tmp_path / "fake-bin" / "docker"
    docker_bin.parent.mkdir()
    docker_bin.write_text(
        r'''#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  info|pull|build) exit 0 ;;
  image) exit 1 ;;
  run)
    repo_root=""
    output_root=""
    junit=""
    image_seen=0
    command=()
    i=2
    while [ "$i" -le "$#" ]; do
      arg="${!i}"
      case "$arg" in
        --rm) ;;
        -v)
          i=$((i + 1))
          mount="${!i}"
          host="${mount%%:*}"
          target="${mount#*:}"
          target="${target%%:*}"
          if [ "$target" = "/repo" ]; then repo_root="$host"; fi
          if [ "$target" = "/static-guard-output" ]; then output_root="$host"; fi
          ;;
        -e)
          i=$((i + 1))
          value="${!i}"
          case "$value" in BLUEPRINT_STATIC_GUARD_JUNIT=*) junit="${value#*=}" ;; esac
          ;;
        *)
          if [ "$image_seen" -eq 0 ]; then
            image_seen=1
          else
            command+=("$arg")
          fi
          ;;
      esac
      i=$((i + 1))
    done
    is_pytest=0
    for arg in "${command[@]}"; do
      [ "$arg" = "pytest" ] && is_pytest=1
    done
    if [ "$is_pytest" -eq 0 ]; then exit 0; fi
    if [ -n "${STATIC_GUARD_CALL_LOG:-}" ]; then
      selectors=""
      pytest_seen=0
      for arg in "${command[@]}"; do
        if [ "$arg" = "pytest" ]; then
          pytest_seen=1
          continue
        fi
        [ "$pytest_seen" -eq 1 ] || continue
        [ -n "$selectors" ] && selectors="$selectors "
        selectors="$selectors$arg"
      done
      printf '%s|%s\n' "$repo_root" "$selectors" >>"$STATIC_GUARD_CALL_LOG"
    fi
    # Model pytest's USAGE error for an unresolvable node id: collection aborts
    # before any test runs and NO JUnit report is written at all. Only checked
    # when the selected file itself exists in this stub tree — the real roster
    # names many files this fixture does not carry.
    for arg in "${command[@]}"; do
      case "$arg" in
        *::*)
          sel_file="${arg%%::*}"
          sel_node="${arg##*::}"
          case "$sel_file" in
            /repo/*) sel_path="$repo_root/${sel_file#/repo/}" ;;
            ../*)    sel_path="$repo_root/${sel_file#../}" ;;
            *)       sel_path="$repo_root/backend/$sel_file" ;;
          esac
          if [ -f "$sel_path" ] && ! grep -q "def $sel_node" "$sel_path"; then
            echo "ERROR: not found: $arg" >&2
            echo "(no match in any of [<Module ${sel_file##*/}>])" >&2
            exit 4
          fi
          ;;
      esac
    done
    # Before the fix, the command receives a host /tmp path but that path is
    # not mounted. Model Docker's disposable container filesystem by refusing
    # to create a host report unless the harness supplies the output mount.
    [ -n "$junit" ] && [ -n "$output_root" ] || exit 0
    if grep -q 'WEAKENED_HARNESS' "$repo_root/scripts/test-backend.sh"; then
      cat >"$output_root/$(basename "$junit")" <<'XML'
<testsuite tests="1" failures="0"><testcase classname="guard" name="clean" /></testsuite>
XML
      exit 0
    fi
    state="$(cat "$repo_root/attribution_state")"
    report="$output_root/$(basename "$junit")"
    if [ "$state" = "clean" ]; then
      cat >"$report" <<'XML'
<testsuite tests="1" failures="0"><testcase classname="guard" name="clean" /></testsuite>
XML
      exit 0
    fi
    # Tree-specific object address + absolute path, so detail normalization is
    # genuinely exercised when the same failure is compared across trees.
    pre_backend="<testcase classname=\"testing.test_existing\" name=\"test_preexisting\"><failure message=\"preexisting\">self = &lt;Test object at 0x$(printf '%x' "${#repo_root}")&gt;
preexisting at $repo_root/backend/testing/test_existing.py</failure></testcase>"
    # Docker classname shape: rootdir is /app, the file is selected as
    # /repo/scripts/..., so the node id is ../repo/scripts/... and the classname
    # keeps the "repo" mount segment.
    pre_scripts="<testcase classname=\"...repo.scripts.test_preexisting_roster\" name=\"test_roster_preexisting\"><failure message=\"preexisting\">preexisting roster failure</failure></testcase>"
    # Collection errors: pytest's mangle_test_address returns a single element,
    # so classname is EMPTY and the dotted module path lands in name. The first
    # names a module present in both trees (resolvable to a file selector); the
    # second names no module at all (unresolvable, must force a full baseline).
    collect_module="<testcase classname=\"\" name=\"testing.test_collect_boom\"><error message=\"collection failure\">ImportError at $repo_root/backend/testing/test_collect_boom.py</error></testcase>"
    collect_opaque="<testcase classname=\"\" name=\"conftest\"><error message=\"collection failure\">conftest import failure</error></testcase>"
    new_roster="<testcase classname=\"...repo.scripts.test_new_roster\" name=\"test_newly_introduced\"><failure message=\"newly introduced\">newly introduced</failure></testcase>"
    if [ "$state" = "collect_base" ]; then
      cat >"$report" <<XML
<testsuite tests="3" failures="3">$pre_backend$collect_module$collect_opaque</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "collect_only" ]; then
      cat >"$report" <<XML
<testsuite tests="1" failures="1">$collect_module</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "collect_mixed" ]; then
      cat >"$report" <<XML
<testsuite tests="3" failures="3">$pre_backend$collect_module$new_roster</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "collect_opaque" ]; then
      cat >"$report" <<XML
<testsuite tests="2" failures="2">$pre_backend$collect_opaque</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "preexisting" ]; then
      cat >"$report" <<XML
<testsuite tests="2" failures="2">$pre_backend$pre_scripts</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "new_node" ]; then
      # The new failure is a node that does not exist in the base tree, inside a
      # roster file that does.
      cat >"$report" <<XML
<testsuite tests="3" failures="3">$pre_backend$pre_scripts<testcase classname="testing.test_existing" name="test_added_here"><failure message="added here">newly introduced node</failure></testcase></testsuite>
XML
      exit 1
    fi
    cat >"$report" <<XML
<testsuite tests="3" failures="3">$pre_backend$pre_scripts$new_roster</testsuite>
XML
    exit 1
    ;;
  *) exit 0 ;;
esac
'''
    )
    os.chmod(docker_bin, 0o755)

    _git(tmp_path, "init", "-b", "automain")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    route_contents = (tmp_path / "scripts" / "backend-test-route.sh").read_bytes()
    (tmp_path / "scripts" / "backend-test-route.sh").unlink()
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "scripts" / "backend-test-route.sh").write_bytes(route_contents)
    _git(tmp_path, "switch", "-c", "task")
    (tmp_path / "attribution_state").write_text(f"{current_state}\n")
    # Current-tree-only sources behind the two new-failure shapes: a brand new
    # roster FILE, and a new NODE inside a roster file that already exists in
    # base. Both must resolve against the current tree and be absent from base.
    (tmp_path / "scripts" / "test_new_roster.py").write_text(
        "def test_newly_introduced():\n    raise AssertionError('fixture only')\n"
    )
    with (tmp_path / "backend" / "testing" / "test_existing.py").open("a") as existing:
        existing.write("\n\ndef test_added_here():\n    raise AssertionError('new node')\n")
    with (tmp_path / "scripts" / "static_release_guard.py").open("a") as guard:
        guard.write("# task changes static-guard path\n")
    _git(tmp_path, "add", "attribution_state", "scripts/static_release_guard.py", "scripts/backend-test-route.sh")
    _git(tmp_path, "commit", "-m", "change static corpus outcome")
    return docker_bin.parent, tmp_path


@pytest.mark.parametrize("current_state, should_fail", [("preexisting", False), ("new", True)])
def test_docker_junit_attribution_distinguishes_existing_and_new_failures(
    tmp_path: Path, current_state: str, should_fail: bool
) -> None:
    """The Docker-backed corpus attributes only failures introduced by the task."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, current_state)
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TASK_ID": "task-introducer-123",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert (result.returncode != 0) is should_fail, output
    if should_fail:
        assert "newly_introduced" in output
        assert '"task_id": "task-introducer-123"' in output
    else:
        assert "no new failures vs base" in output


def test_identical_failure_with_tree_specific_path_is_preexisting(tmp_path: Path) -> None:
    """A traceback path difference alone must not make an old failure new."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "preexisting")
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TASK_ID": "task-existing-failure-123",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "no new failures vs base" in output


@pytest.mark.parametrize(
    ("current_state", "expected_corpus_runs"),
    [("clean", 1), ("preexisting", 2)],
)
def test_baseline_corpus_is_lazy_after_current_tree_result(
    tmp_path: Path, current_state: str, expected_corpus_runs: int
) -> None:
    """The baseline runs only when current-tree failures need subtraction."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, current_state)
    call_log = tmp_path / "corpus-calls.log"
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "STATIC_GUARD_CALL_LOG": str(call_log),
            "TASK_ID": "task-lazy-baseline-123",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert len(call_log.read_text().splitlines()) == expected_corpus_runs


def test_changed_harness_cannot_hide_a_base_corpus_failure(tmp_path: Path) -> None:
    """A weakened candidate harness is compared against the untouched base harness."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "preexisting")
    harness = repo / "scripts" / "test-backend.sh"
    harness.write_text(harness.read_text() + "\n# WEAKENED_HARNESS\n")
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TASK_ID": "task-harness-weakening-123",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "changed scripts/test-backend.sh made base corpus" in output


def test_baseline_scopes_to_current_failures_and_survives_roster_drift(
    tmp_path: Path,
) -> None:
    """Only base-present failed nodes are selected for subtraction."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "new")
    call_log = tmp_path / "corpus-calls.log"
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "STATIC_GUARD_CALL_LOG": str(call_log),
            "TASK_ID": "task-roster-drift-123",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    assert calls[0].startswith("testing/test_security.py")
    # Scoped to the FILES that failed in the current tree and exist in base —
    # never the full corpus, and never a node id (see the new-node test below).
    baseline = [arg for arg in calls[1].split() if not arg.startswith("-")]
    assert baseline == [
        "/repo/scripts/test_preexisting_roster.py",
        "testing/test_existing.py",
    ], baseline
    assert "test_new_roster" not in calls[1]
    assert "newly_introduced" in output
    assert "standalone static corpus attribution" not in output
    # Only the new file's failure is charged; the two pre-existing ones (one of
    # them a Docker-shape scripts/ classname) are subtracted.
    assert "test_preexisting" not in output.split("new failures:", 1)[1].split("\n")[0]
    assert "test_roster_preexisting" not in output.split("new failures:", 1)[1].split("\n")[0]


def test_new_node_in_existing_roster_file_is_attributed_without_usage_error(
    tmp_path: Path,
) -> None:
    """A NEW node inside a base-present roster file must not collapse the base run.

    Node-level base selection would hand pytest a node id that does not exist in
    the base tree ("ERROR: not found: ..."), aborting collection before any
    JUnit report is written — which the guard would then report as an
    unattributable corpus failure naming no test at all.
    """
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "new_node")
    call_log = tmp_path / "corpus-calls.log"
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "STATIC_GUARD_CALL_LOG": str(call_log),
            "TASK_ID": "task-new-node-123",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    # The base run selects the containing FILE, so it always resolves.
    assert "::" not in calls[1], calls[1]
    assert "testing/test_existing.py" in calls[1]
    assert "ERROR: not found" not in output
    # The new node is named; the pre-existing failures in the same file are not.
    assert "standalone static corpus attribution" not in output
    new_line = output.split("new failures:", 1)[1].split("\n")[0]
    assert "test_added_here" in new_line
    assert "test_preexisting" not in new_line
    assert '"task_id": "task-new-node-123"' in new_line


def _run_attribution_guard(
    repo: Path, fake_bin: Path, *, task_id: str, call_log: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TASK_ID": task_id,
    }
    if call_log is not None:
        env["STATIC_GUARD_CALL_LOG"] = str(call_log)
    return subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_sole_collection_error_is_subtracted_against_base(tmp_path: Path) -> None:
    """A collection error present in BOTH trees must not be charged as new.

    pytest reports a collection error with an EMPTY classname and the dotted
    module path in ``name``. A classname-only resolver yields no selector, the
    key never reaches the scoped baseline, and — when it is the ONLY failure —
    the empty-selector branch skips the base run entirely and charges every
    current failure as new. The full-corpus baseline used to subtract it.
    """
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "collect_only", base_state="collect_base"
    )
    call_log = tmp_path / "corpus-calls.log"
    result = _run_attribution_guard(
        repo, fake_bin, task_id="task-collect-only-123", call_log=call_log
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "no new failures vs base" in output
    assert "new failures:" not in output
    # The baseline actually ran — the skip-the-base-run branch must not trigger.
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    assert len(calls) == 2, calls
    assert "testing/test_collect_boom.py" in calls[1]
    assert "::" not in calls[1], calls[1]


def test_collection_error_alongside_resolvable_failure_charges_only_the_new_one(
    tmp_path: Path,
) -> None:
    """The empty-classname shape must be baselined next to ordinary failures."""
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "collect_mixed", base_state="collect_base"
    )
    call_log = tmp_path / "corpus-calls.log"
    result = _run_attribution_guard(
        repo, fake_bin, task_id="task-collect-mixed-123", call_log=call_log
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    baseline = [arg for arg in calls[1].split() if not arg.startswith("-")]
    assert baseline == [
        "testing/test_collect_boom.py",
        "testing/test_existing.py",
    ], baseline
    assert "standalone static corpus attribution" not in output
    new_line = output.split("new failures:", 1)[1].split("\n")[0]
    assert "test_newly_introduced" in new_line
    assert "test_collect_boom" not in new_line
    assert "test_preexisting" not in new_line


def test_unresolvable_failure_id_falls_back_to_full_base_corpus(
    tmp_path: Path,
) -> None:
    """A failure whose selector cannot be derived forces an UNSCOPED baseline.

    Without the fallback the key is missing from the scoped base report and is
    charged as new even though base fails identically.
    """
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "collect_opaque", base_state="collect_base"
    )
    call_log = tmp_path / "corpus-calls.log"
    result = _run_attribution_guard(
        repo, fake_bin, task_id="task-collect-opaque-123", call_log=call_log
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "no new failures vs base" in output
    assert "unresolvable failure id(s)" in output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    baseline = [arg for arg in calls[1].split() if not arg.startswith("-")]
    current = [arg for arg in calls[0].split() if not arg.startswith("-")]
    # Unscoped: the baseline runs the same roster as the current tree, including
    # files that did not fail — not the scoped two-file subset.
    assert baseline == current, baseline
    assert "testing/test_security.py" in baseline


def test_docker_classname_repo_mount_resolves_to_scripts_path() -> None:
    """The /repo bind-mount segment must not defeat classname->path resolution."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "static_release_guard", REPO_ROOT / "scripts" / "static_release_guard.py"
    )
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    # Docker: rootdir /app, file selected as /repo/scripts/... -> node id
    # ../repo/scripts/test_runner_isolation.py -> classname "...repo.scripts...".
    selector = guard._pytest_selector(
        "...repo.scripts.test_runner_isolation", "test_x", tree_root=REPO_ROOT
    )
    assert selector is not None
    assert selector[0] == "scripts/test_runner_isolation.py::test_x"
    # Non-Docker: ../scripts/test_runner_isolation.py -> "...scripts...".
    selector = guard._pytest_selector(
        "...scripts.test_runner_isolation", "test_x", tree_root=REPO_ROOT
    )
    assert selector is not None
    assert selector[0] == "scripts/test_runner_isolation.py::test_x"
    # Backend tests are rootdir-relative and keep resolving unchanged.
    selector = guard._pytest_selector(
        "testing.test_invariants", "test_x", tree_root=REPO_ROOT
    )
    assert selector is not None
    assert selector[0] == "testing/test_invariants.py::test_x"


def test_new_static_finding_without_task_identity_fails_closed(tmp_path: Path) -> None:
    """A new finding must not be downgraded to an unknown task owner."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "new")
    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    environment.pop("TASK_ID", None)
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "TASK_ID is missing" in output
    assert "newly_introduced" in output
