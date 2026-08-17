"""Regression coverage for running static release guards on the task diff."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backend_verification_guard  # noqa: E402
import static_release_guard  # noqa: E402


def test_static_guard_base_unpack_uses_data_filter() -> None:
    """The shared git-archive unpack still uses an explicit safe filter."""
    source = (REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py").read_text()
    assert 'bundle.extractall(destination, filter="data")' in source


def test_static_scope_matches_path_boundaries() -> None:
    assert static_release_guard._is_static_scope("backend/testing/test_guard.py")
    assert static_release_guard._is_static_scope("check")
    assert not static_release_guard._is_static_scope("checklist/results.txt")
    assert not static_release_guard._is_static_scope("scripts/test-backend.sh.bak")


class _OrderedStderr:
    def __init__(self, process: _ArchiveProcess) -> None:
        self._process = process

    def read(self) -> bytes:
        assert self._process.waited, "stderr must be read only after the archive exits"
        return b""


class _ArchiveProcess:
    def __init__(self, archive_bytes: bytes) -> None:
        self.stdout = io.BytesIO(archive_bytes)
        self.waited = False
        self.stderr = _OrderedStderr(self)

    def wait(self) -> int:
        self.waited = True
        return 0


def _archive_bytes() -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        content = b"archive payload\n"
        member = tarfile.TarInfo("payload.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return payload.getvalue()


@pytest.mark.parametrize(
    "extractor",
    [static_release_guard._extract_base, backend_verification_guard._extract_base],
)
def test_archive_extractors_drain_stderr_without_a_pipe_or_read_deadlock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extractor,
) -> None:
    process = _ArchiveProcess(_archive_bytes())
    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        assert kwargs["stderr"] is not subprocess.PIPE
        return process

    monkeypatch.setattr(static_release_guard.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(backend_verification_guard.subprocess, "Popen", fake_popen)

    destination = tmp_path / "destination"
    destination.mkdir()
    extractor(tmp_path / "repo", "automain", destination)

    assert process.waited
    assert (destination / "payload.txt").read_bytes() == b"archive payload\n"
    assert len(popen_calls) == 1


def test_check_runs_static_guard_only_for_test_gate() -> None:
    """The dispatcher reaches the tracked fallback, whose guard stays test-only."""
    dispatcher = (REPO_ROOT / "check").read_text()
    assert 'DEFAULT_GATE="$WORKSPACE/scripts/check-default.sh"' in dispatcher
    assert 'if [ ! -x "$DEFAULT_GATE" ]; then' in dispatcher
    assert 'exec "$DEFAULT_GATE" "$@"' in dispatcher

    fallback = (REPO_ROOT / "scripts" / "check-default.sh").read_text()
    release_block_start = fallback.index("# The database-free release guard corpus")
    release_block_end = fallback.index("# Idempotent npm install", release_block_start)
    release_block = fallback[release_block_start:release_block_end]
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


_CLEAN_VERIFICATION_JUNIT = (
    '<testsuite tests="1" failures="0">'
    '<testcase classname="testing.test_x" name="test_passes" />'
    "</testsuite>"
)
_FAILING_VERIFICATION_JUNIT = (
    '<testsuite tests="1" failures="1">'
    '<testcase classname="testing.test_x" name="test_fails">'
    '<failure message="boom">boom</failure>'
    "</testcase></testsuite>"
)


def _run_backend_guard_with_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_junit_text: str,
    current_exit_code: int,
    baseline_junit_text: str,
    baseline_exit_code: int,
) -> dict[str, object]:
    repo = tmp_path / "repo"
    base_tree = tmp_path / "base"
    harness = base_tree / "scripts" / "test-backend.sh"
    route_helper = base_tree / "scripts" / "backend-test-route.sh"
    selector = base_tree / "backend" / "testing" / "test_x.py"
    repo.mkdir()
    current_selector = repo / "backend" / "testing" / "test_x.py"
    current_selector.parent.mkdir(parents=True)
    current_selector.write_text("def test_passes(): assert True\n", encoding="utf-8")
    harness.parent.mkdir(parents=True)
    selector.parent.mkdir(parents=True)
    harness.write_text(
        "cat > \"$BLUEPRINT_VERIFICATION_JUNIT\" <<'XML'\n"
        f"{baseline_junit_text}\n"
        "XML\n"
        f"exit {baseline_exit_code}\n",
        encoding="utf-8",
    )
    route_helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    selector.write_text("def test_passes(): assert True\n", encoding="utf-8")

    monkeypatch.setattr(
        backend_verification_guard, "_base_ref", lambda _repo, _base: "automain"
    )

    def extract_base(_repo: Path, _ref: str, destination: Path) -> None:
        shutil.copytree(base_tree, destination, dirs_exist_ok=True)

    monkeypatch.setattr(backend_verification_guard, "_extract_base", extract_base)

    current_junit = tmp_path / "current.xml"
    current_junit.write_text(current_junit_text, encoding="utf-8")
    exit_code = backend_verification_guard.main(
        [
            "--base=automain",
            f"--repo-root={repo}",
            f"--current-junit={current_junit}",
            f"--current-exit-code={current_exit_code}",
            "--route=--sandbox-postgres",
            "--harness=scripts/test-backend.sh",
            "testing/test_x.py",
        ]
    )

    assert exit_code == 0
    return json.loads(capsys.readouterr().out)


def test_backend_guard_blocks_clean_junit_after_nonzero_current_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-JUnit current harness failure can never be attributed away."""

    outcome = _run_backend_guard_with_baseline(
        tmp_path,
        capsys,
        monkeypatch,
        current_junit_text=_CLEAN_VERIFICATION_JUNIT,
        current_exit_code=1,
        baseline_junit_text=_CLEAN_VERIFICATION_JUNIT,
        baseline_exit_code=0,
    )

    assert outcome["state"] == "execution_failure"
    assert outcome["blocking"] is True
    assert outcome["current_exit_code"] == 1
    assert outcome["current_failures"] == {}
    assert outcome["baseline_selectors"] == ["testing/test_x.py"]


@pytest.mark.parametrize(
    ("baseline_junit_text", "baseline_exit_code"),
    [
        (_CLEAN_VERIFICATION_JUNIT, 1),
        (_FAILING_VERIFICATION_JUNIT, 0),
        (_FAILING_VERIFICATION_JUNIT, 2),
        (_CLEAN_VERIFICATION_JUNIT, 137),
    ],
)
def test_backend_guard_rejects_invalid_baseline_exit_report_pair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    baseline_junit_text: str,
    baseline_exit_code: int,
) -> None:
    outcome = _run_backend_guard_with_baseline(
        tmp_path,
        capsys,
        monkeypatch,
        current_junit_text=_CLEAN_VERIFICATION_JUNIT,
        current_exit_code=0,
        baseline_junit_text=baseline_junit_text,
        baseline_exit_code=baseline_exit_code,
    )

    assert outcome["state"] == "baseline_unavailable"
    assert outcome["blocking"] is True
    assert outcome["base_exit_code"] == baseline_exit_code
    assert "exit code/JUnit pair is not attributable" in outcome["error"]


def test_backend_guard_accepts_exit_one_matching_baseline_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run_backend_guard_with_baseline(
        tmp_path,
        capsys,
        monkeypatch,
        current_junit_text=_FAILING_VERIFICATION_JUNIT,
        current_exit_code=1,
        baseline_junit_text=_FAILING_VERIFICATION_JUNIT,
        baseline_exit_code=1,
    )

    assert outcome["state"] == "preexisting_test_failure"
    assert outcome["blocking"] is False
    assert outcome["new_failures"] == {}


def _prepare_backend_verification_docker_harness(tmp_path: Path) -> tuple[Path, Path]:
    """Build a tiny checkout plus a Docker argv-preserving test double."""

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "backend" / "testing").mkdir(parents=True)
    for relative in ("test-backend.sh", "backend-test-route.sh"):
        destination = repo / "scripts" / relative
        destination.write_text(
            (REPO_ROOT / "scripts" / relative).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        os.chmod(destination, 0o755)
    (repo / "backend" / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (repo / "backend" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repo / "backend" / "testing" / "test_x.py").write_text(
        "def test_passes():\n    assert True\n", encoding="utf-8"
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        r'''#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  info|pull|tag|network|rm) exit 0 ;;
  image) exit 0 ;;
  exec)
    for arg in "$@"; do
      if [ "$arg" = "psql" ]; then printf '1\n'; break; fi
    done
    exit 0
    ;;
  run)
    shift
    detached=0
    image_seen=0
    output_root=""
    command=()
    while [ "$#" -gt 0 ]; do
      arg="$1"
      shift
      case "$arg" in
        -d) detached=1 ;;
        --rm) ;;
        -v)
          mount="$1"
          shift
          case "$mount" in
            *:/verification-output) output_root="${mount%:/verification-output}" ;;
          esac
          ;;
        -e|--name|--network) shift ;;
        *)
          if [ "$image_seen" -eq 0 ]; then
            image_seen=1
          else
            command+=("$arg")
          fi
          ;;
      esac
    done
    [ "$detached" -eq 0 ] || exit 0
    junit=""
    for arg in "${command[@]}"; do
      case "$arg" in --junitxml=*) junit="${arg#*=}" ;; esac
    done
    [ -n "$junit" ] || exit 0
    [ -n "$output_root" ] || {
      echo "verification output mount missing" >&2
      exit 40
    }
    case "$junit" in
      /verification-output/*) junit_name="${junit#/verification-output/}" ;;
      *) echo "unmapped JUnit path: $junit" >&2; exit 41 ;;
    esac
    printf '%s\n' "$output_root" > "$FAKE_DOCKER_VERIFICATION_MOUNT_LOG"
    cat > "$output_root/$junit_name" <<'XML'
<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="testing.test_x" name="test_passes" /></testsuite>
XML
    exit 0
    ;;
  *) exit 0 ;;
esac
''',
        encoding="utf-8",
    )
    os.chmod(docker, 0o755)
    return repo, fake_bin


@pytest.mark.parametrize("path_kind", ["relative", "whitespace"])
def test_backend_verification_docker_preserves_configured_junit_path(
    tmp_path: Path, path_kind: str
) -> None:
    """Docker writes the configured filename into the exact canonical host dir."""

    repo, fake_bin = _prepare_backend_verification_docker_harness(tmp_path)
    if path_kind == "relative":
        configured = Path("relative-results") / "selected-current.xml"
        expected = repo / "backend" / configured
    else:
        expected = tmp_path / "results with spaces" / "selected-current.xml"
        configured = expected
    expected.parent.mkdir(parents=True)
    receipt = tmp_path / f"{path_kind}-receipt.json"
    mount_log = tmp_path / f"{path_kind}-mount.log"

    result = subprocess.run(
        [
            "bash",
            str(repo / "scripts" / "test-backend.sh"),
            "--postgres",
            "testing/test_x.py",
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "BLUEPRINT_VERIFICATION_JUNIT": str(configured),
            "BLUEPRINT_VERIFICATION_RESULT_FILE": str(receipt),
            "FAKE_DOCKER_VERIFICATION_MOUNT_LOG": str(mount_log),
            "TESTMON_NO_FETCH": "1",
            "USE_TEST_POSTGRES": "true",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert expected.is_file(), output
    assert mount_log.read_text(encoding="utf-8").strip() == str(
        expected.parent.resolve()
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["selectors"] == ["testing/test_x.py"]
    assert payload["tests"] == 1
    assert payload["failures"] == 0


def test_task_diff_breaching_size_guard_fails_check(tmp_path: Path) -> None:
    """A changed cap that makes a source file over-limit must fail the gate."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "testing").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "check", tmp_path / "check")
    shutil.copy2(
        REPO_ROOT / "scripts" / "check-default.sh",
        tmp_path / "scripts" / "check-default.sh",
    )
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
    os.chmod(tmp_path / "scripts" / "check-default.sh", 0o755)
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
        env={**os.environ, "WORKSPACE": str(tmp_path), "TASK_ID": ""},
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
    guard_selectors=""
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
          case "$value" in
            BLUEPRINT_STATIC_GUARD_JUNIT=*) junit="${value#*=}" ;;
            BLUEPRINT_STATIC_GUARD_SELECTORS=*) guard_selectors="${value#*=}" ;;
          esac
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
    selector_enabled() {
      [ -z "$guard_selectors" ] && return 0
      printf '%s' "$guard_selectors" | base64 -d 2>/dev/null | grep -Fxq -- "$1"
    }
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
    selected_pre_backend=""
    selected_pre_scripts=""
    selected_collect_module=""
    selected_collect_opaque=""
    selected_failure_count=0
    if selector_enabled "testing/test_existing.py"; then
      selected_pre_backend="$pre_backend"
      selected_failure_count=$((selected_failure_count + 1))
    fi
    if selector_enabled "scripts/test_preexisting_roster.py"; then
      selected_pre_scripts="$pre_scripts"
      selected_failure_count=$((selected_failure_count + 1))
    fi
    if selector_enabled "testing/test_collect_boom.py"; then
      selected_collect_module="$collect_module"
    fi
    # The opaque collection failure has no file selector. It is visible only
    # to a genuinely full corpus run, exactly like the production fallback.
    if [ -z "$guard_selectors" ]; then
      selected_collect_opaque="$collect_opaque"
    fi
    if [ "$state" = "collect_base" ]; then
      selected_collect_count=0
      [ -n "$selected_collect_module" ] && selected_collect_count=$((selected_collect_count + 1))
      [ -n "$selected_collect_opaque" ] && selected_collect_count=$((selected_collect_count + 1))
      selected_tests=$((selected_failure_count + selected_collect_count))
      cat >"$report" <<XML
<testsuite tests="$selected_tests" failures="$selected_tests">$selected_pre_backend$selected_pre_scripts$selected_collect_module$selected_collect_opaque</testsuite>
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
      if [ "$selected_failure_count" -eq 0 ]; then
        cat >"$report" <<'XML'
<testsuite tests="1" failures="0"><testcase classname="guard" name="clean" /></testsuite>
XML
        exit 0
      fi
      cat >"$report" <<XML
<testsuite tests="$selected_failure_count" failures="$selected_failure_count">$selected_pre_backend$selected_pre_scripts</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "partial_preexisting" ]; then
      cat >"$report" <<XML
<testsuite tests="1" failures="1">$pre_backend</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "partial_skipped" ]; then
      pre_scripts_skipped="<testcase classname=\"...repo.scripts.test_preexisting_roster\" name=\"test_roster_preexisting\"><skipped message=\"harness selection\" /></testcase>"
      cat >"$report" <<XML
<testsuite tests="2" failures="1" skipped="1">$pre_backend$pre_scripts_skipped</testsuite>
XML
      exit 1
    fi
    case "$state" in
      preexisting_exit_*)
        abnormal_exit="${state#preexisting_exit_}"
        cat >"$report" <<XML
<testsuite tests="$selected_failure_count" failures="$selected_failure_count">$selected_pre_backend$selected_pre_scripts</testsuite>
XML
        exit "$abnormal_exit"
        ;;
    esac
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
    assert "[baseline] governance findings unavailable" not in output
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


@pytest.mark.parametrize("abnormal_exit", [0, 2, 3, 4, 5, 137])
@pytest.mark.parametrize("invalid_side", ["current", "baseline"])
def test_static_guard_rejects_invalid_exit_junit_pair_before_attribution(
    tmp_path: Path, abnormal_exit: int, invalid_side: str
) -> None:
    """An incomplete current or baseline run cannot subtract matching failures."""

    invalid_state = f"preexisting_exit_{abnormal_exit}"
    current_state = invalid_state if invalid_side == "current" else "preexisting"
    base_state = invalid_state if invalid_side == "baseline" else "preexisting"
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, current_state, base_state=base_state
    )

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id=f"task-invalid-{invalid_side}-{abnormal_exit}",
    )

    output = result.stdout + result.stderr
    expected_authority = (
        "execution_failure" if invalid_side == "current" else "baseline_unavailable"
    )
    assert result.returncode != 0, output
    assert expected_authority in output
    assert "pytest exit code/JUnit pair is not attributable" in output
    assert "no new failures vs base" not in output
    assert "[static guard] PASS" not in output


@pytest.mark.parametrize(
    ("current_state", "base_state"),
    [("clean", "clean"), ("preexisting", "preexisting")],
)
def test_static_guard_accepts_legitimate_clean_and_exit_one_pairs(
    tmp_path: Path, current_state: str, base_state: str
) -> None:
    """Exit zero/clean and exit one/failing remain valid attribution evidence."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, current_state, base_state=base_state
    )

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id=f"task-valid-{current_state}",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "execution_failure" not in output
    assert "baseline_unavailable" not in output
    assert "[static guard] PASS" in output


def test_changed_harness_clean_current_rejects_invalid_full_baseline_pair(
    tmp_path: Path,
) -> None:
    """The clean-current harness audit validates its full baseline authority."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path,
        "clean",
        base_state="preexisting_exit_2",
    )
    harness = repo / "scripts" / "test-backend.sh"
    harness.write_text(harness.read_text() + "\n# current harness changed\n")

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-clean-current-invalid-baseline",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "baseline_unavailable" in output
    assert "pytest exit code/JUnit pair is not attributable" in output
    assert "no new failures vs base" not in output
    assert "[static guard] PASS" not in output


def test_changed_harness_invocation_cannot_hide_base_corpus_failure(
    tmp_path: Path,
) -> None:
    """Invocation weakening outside the roster fingerprint remains blocking."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path,
        "clean",
        base_state="preexisting",
    )
    harness = repo / "scripts" / "test-backend.sh"
    original = harness.read_text(encoding="utf-8")
    invocation = (
        '  _run python3 -m pytest "${STANDALONE_ARGS[@]}" -v --noconftest \\\n'
    )
    weakened = (
        "  # WEAKENED_HARNESS: invocation-only selection change\n"
        '  _run python3 -m pytest "${STANDALONE_ARGS[0]}" -v --noconftest \\\n'
    )
    assert invocation in original
    harness.write_text(original.replace(invocation, weakened, 1), encoding="utf-8")

    base_harness = tmp_path / "base-test-backend.sh"
    base_harness.write_text(
        subprocess.run(
            ["git", "show", "automain:scripts/test-backend.sh"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
        encoding="utf-8",
    )
    assert not static_release_guard._static_roster_changed(base_harness, harness)

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-invocation-weakening",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "changed static test harness made base corpus failure(s) disappear" in output
    assert "[static guard] PASS" not in output


@pytest.mark.parametrize(
    ("current_state", "expected_reason"),
    [
        ("partial_preexisting", "failure(s) disappear"),
        ("partial_skipped", "failure(s) become skipped"),
    ],
)
def test_changed_harness_partial_invocation_cannot_hide_base_corpus_failure(
    tmp_path: Path,
    current_state: str,
    expected_reason: str,
) -> None:
    """A retained old failure cannot conceal another omitted baseline failure."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path,
        current_state,
        base_state="preexisting",
    )
    harness = repo / "scripts" / "test-backend.sh"
    original = harness.read_text(encoding="utf-8")
    invocation = (
        '  _run python3 -m pytest "${STANDALONE_ARGS[@]}" -v --noconftest \\\n'
    )
    weakened = (
        "  # PARTIAL_SELECTION_CHANGE: invocation-only weakening\n"
        '  _run python3 -m pytest "${STANDALONE_ARGS[@]:0:1}" -v --noconftest \\\n'
    )
    assert invocation in original
    harness.write_text(original.replace(invocation, weakened, 1), encoding="utf-8")

    base_harness = tmp_path / "base-test-backend.sh"
    base_harness.write_text(
        subprocess.run(
            ["git", "show", "automain:scripts/test-backend.sh"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
        encoding="utf-8",
    )
    assert not static_release_guard._static_roster_changed(base_harness, harness)

    call_log = tmp_path / f"partial-{current_state}-calls.log"
    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id=f"task-partial-invocation-weakening-{current_state}",
        call_log=call_log,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert expected_reason in output
    assert "[static guard] PASS" not in output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    assert len(calls) == 2
    # A is the surviving backend failure; B is the hidden scripts failure.
    # The fake Docker exposes A+B when the guard leaves the base run unscoped;
    # those fixture paths are not literal members of the production static
    # roster.  Prove the untouched base harness ran its broader roster instead
    # of inheriting the candidate's one-selector weakening.
    current = [arg for arg in calls[0].split() if not arg.startswith("-")]
    baseline = [arg for arg in calls[1].split() if not arg.startswith("-")]
    assert len(current) == 1, current
    assert set(current) < set(baseline), baseline


def test_unchanged_harness_clean_current_keeps_lazy_base_control(
    tmp_path: Path,
) -> None:
    """A clean current corpus does not charge an unexecuted unchanged baseline."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path,
        "clean",
        base_state="preexisting",
    )
    call_log = tmp_path / "unchanged-harness-calls.log"

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-unchanged-harness-control",
        call_log=call_log,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert len(call_log.read_text(encoding="utf-8").splitlines()) == 1
    assert "<harness>" not in output
    assert "[static guard] PASS" in output


def test_unchanged_harness_failing_current_keeps_scoped_baseline_control(
    tmp_path: Path,
) -> None:
    """Only an unchanged harness may scope base work to surviving failure A."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path,
        "partial_preexisting",
        base_state="preexisting",
    )
    call_log = tmp_path / "unchanged-failing-harness-calls.log"

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-unchanged-failing-harness-control",
        call_log=call_log,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    assert len(calls) == 2
    baseline = [arg for arg in calls[1].split() if not arg.startswith("-")]
    assert baseline == ["testing/test_existing.py"], baseline
    assert "/repo/scripts/test_preexisting_roster.py" not in calls[1]
    assert "no new failures vs base" in output
    assert "[static guard] PASS" in output


@pytest.mark.parametrize(
    ("current_state", "expected_corpus_runs", "expected_returncode"),
    [("clean", 1, 0), ("preexisting", 2, 0)],
)
def test_baseline_corpus_is_lazy_after_current_tree_result(
    tmp_path: Path, current_state: str, expected_corpus_runs: int, expected_returncode: int
) -> None:
    """The baseline runs only when current failures need subtraction."""
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
    assert result.returncode == expected_returncode, output
    assert len(call_log.read_text().splitlines()) == expected_corpus_runs


def test_changed_harness_cannot_hide_a_base_corpus_failure(tmp_path: Path) -> None:
    """A weakened candidate harness is compared against the untouched base harness."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "preexisting")
    harness = repo / "scripts" / "test-backend.sh"
    harness.write_text(
        harness.read_text().replace(
            'CI_SCRIPT_TEST_DISCOVERY_GLOB="test_*.py"',
            'CI_SCRIPT_TEST_DISCOVERY_GLOB="test_changed_*.py"',
        )
        + "\n# WEAKENED_HARNESS\n"
    )
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
    assert "changed static test roster made base corpus" in output


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


def test_sole_collection_error_is_subtracted_without_blocking_missing_routes(tmp_path: Path) -> None:
    """A shared collection error is suppressed; other vanished routes do not block.

    pytest reports a collection error with an EMPTY classname and the dotted
    module path in ``name``. The shared resolver translates that shape to the
    containing file, so the baseline stays scoped while unrelated vanished
    routes remain non-blocking.
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
    # The baseline actually ran and stayed scoped to the collection-error file.
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    assert len(calls) == 2, calls
    assert "testing/test_collect_boom.py" in calls[1], calls[1]


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
    assert "testing/test_collect_boom.py" in baseline
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


def test_shared_report_resolver_handles_docker_and_collection_shapes() -> None:
    """The shared resolver handles both mounted paths and collection errors."""
    from testing.fixgate_attribution import _resolve_route

    # Docker: rootdir /app, file selected as /repo/scripts/... -> node id
    # ../repo/scripts/test_runner_isolation.py -> classname "...repo.scripts...".
    route, testcase, source = _resolve_route(
        "...repo.scripts.test_runner_isolation", "test_x", tree_root=REPO_ROOT
    )
    assert route == "scripts/test_runner_isolation.py"
    assert testcase == "test_x"
    assert source is not None
    # Non-Docker: ../scripts/test_runner_isolation.py -> "...scripts...".
    route, testcase, _ = _resolve_route(
        "...scripts.test_runner_isolation", "test_x", tree_root=REPO_ROOT
    )
    assert route == "scripts/test_runner_isolation.py"
    assert testcase == "test_x"
    # Empty-classname collection errors resolve to a file-level route.
    route, testcase, _ = _resolve_route(
        "", "testing.test_invariants", tree_root=REPO_ROOT
    )
    assert route == "backend/testing/test_invariants.py"
    assert testcase == ""


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
