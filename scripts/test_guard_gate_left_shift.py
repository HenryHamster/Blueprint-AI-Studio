"""Regression coverage for running static release guards on the task diff."""

from __future__ import annotations

import io
import json
import os
import shlex
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
import guard_suppression_receipt  # noqa: E402
import static_release_guard  # noqa: E402
import typecheck_verification_guard  # noqa: E402
import vitest_verification_guard  # noqa: E402
from testing.fixgate_attribution import (  # noqa: E402
    classify_verification,
    format_offender_marker,
)


def _offender_detail(items) -> str:
    """Render a guard's offender collection with a stable machine identity.

    The prose stays readable; the appended marker gives the differential
    attributor a set identity so an inherited breach whose membership or
    ordering shifts is not charged to a diff that introduced nothing.
    """

    rendered = [str(item) for item in items]
    identities = [line.replace("\n", " ").strip() or "<empty>" for line in rendered]
    return " | ".join(rendered) + "\n" + format_offender_marker(identities)


def test_static_guard_base_unpack_uses_data_filter() -> None:
    """The shared git-archive unpack still uses an explicit safe filter."""
    source = (REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py").read_text()
    assert 'bundle.extractall(destination, filter="data")' in source


def test_static_scope_matches_path_boundaries() -> None:
    assert static_release_guard._is_static_scope("backend/testing/test_guard.py")
    assert static_release_guard._is_static_scope("check")
    assert not static_release_guard._is_static_scope("checklist/results.txt")
    assert not static_release_guard._is_static_scope("backend-other/foo.py")
    # scripts/ is a directory prefix (manifest-derived) as of this guard, so a
    # sibling file under scripts/ is legitimately in scope, not a boundary leak.
    assert static_release_guard._is_static_scope("scripts/test-backend.sh.bak")
    assert not static_release_guard._is_static_scope("scripts-other/foo.py")


def test_static_scope_covers_manifest_protected_directories() -> None:
    """Guard selection follows what the Workday manifest reads, not only where
    the guard itself historically lived — most manifest-protected paths are
    outside backend/ (admin-ui/, agent/, scripts/)."""
    manifest = json.loads(
        (REPO_ROOT / "backend" / "testing" / "conversational_workday_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    top_level_dirs = {
        entry["path"].split("/", 1)[0]
        for entry in manifest["protect"]
        if "/" in entry["path"]
    }
    assert {"admin-ui", "agent", "scripts"} <= top_level_dirs
    assert static_release_guard._is_static_scope(
        "admin-ui/src/components/StrategicSessionControl.tsx"
    )
    assert static_release_guard._is_static_scope("agent/src/handlers/strategic.ts")
    assert not static_release_guard._is_static_scope("docs/some_unrelated_doc.md")


def test_manifest_protected_prefixes_falls_back_when_manifest_unusable(
    tmp_path: Path,
) -> None:
    """A missing or malformed manifest yields no extra prefixes, not a crash."""
    missing = tmp_path / "does-not-exist.json"
    assert static_release_guard._manifest_protected_prefixes(missing) == ()

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not valid json", encoding="utf-8")
    assert static_release_guard._manifest_protected_prefixes(malformed) == ()


def test_static_scope_reaches_admin_ui_via_drift_sentinel_manifest() -> None:
    """A path outside STATIC_PATH_PREFIXES enters scope via the surface manifest.

    Before this, an admin-ui-only PR matched neither the reverse-dep scan nor
    STATIC_PATH_PREFIXES, so the standalone corpus (which contains the guards
    whose inputs actually live under admin-ui/ and agent/, e.g.
    test_vitest_durations_freshness.py) never ran on that PR.
    """
    assert static_release_guard._is_static_scope("admin-ui/src/components/MessagesPanel.tsx")
    assert static_release_guard._is_static_scope("agent/src/handlers/strategic.ts")
    # A path outside every registered surface glob stays out of scope.
    assert not static_release_guard._is_static_scope("docs/README.md")


def test_static_scope_is_static_scope_default_repo_root_matches_live_manifest() -> None:
    """The single-arg call form (used elsewhere in this module) still resolves
    against the live repo's manifest, not an empty/omitted root."""
    assert static_release_guard._is_static_scope("admin-ui/src/App.tsx")


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
            # This test is about the DOCKER route: it puts a fake `docker` on
            # PATH and asserts the mount/JUnit contract the docker branch
            # implements. But `--postgres` auto-upgrades to the off-image
            # pgserver sandbox whenever select_backend_test_route finds one
            # (test-backend.sh: the `_AUTO_POSTGRES_FLAG = --sandbox-postgres`
            # branch), and BLUEPRINT_SANDBOX_PYTHON is inherited through
            # **os.environ. On any host that provisions a pgserver sandbox the
            # harness therefore took the sandbox branch, never reached the fake
            # docker, and this test failed while appearing to cover the docker
            # path. Point the sandbox interpreter at a path that does not exist
            # so the route falls through to docker, which is what is under test.
            "BLUEPRINT_SANDBOX_PYTHON": str(tmp_path / "absent-sandbox-python"),
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
    # See _prepare_docker_attribution_repo: without __init__.py, PEP 420
    # namespace-package resolution can shadow this fixture's backend/app with
    # the real checkout's regular package.
    (tmp_path / "backend" / "app" / "__init__.py").write_text("")
    shutil.copy2(REPO_ROOT / "check", tmp_path / "check")
    shutil.copy2(
        REPO_ROOT / "scripts" / "check-default.sh",
        tmp_path / "scripts" / "check-default.sh",
    )
    shutil.copy2(
        REPO_ROOT / "scripts" / "static_release_guard.py",
        tmp_path / "scripts" / "static_release_guard.py",
    )
    # The guard imports its sibling suppression-receipt emitter, so the fixture
    # tree must carry it too.
    shutil.copy2(
        REPO_ROOT / "scripts" / "guard_suppression_receipt.py",
        tmp_path / "scripts" / "guard_suppression_receipt.py",
    )
    shutil.copy2(
        REPO_ROOT / "scripts" / "verification_receipt.py",
        tmp_path / "scripts" / "verification_receipt.py",
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
        REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py",
        tmp_path / "backend" / "testing" / "fixgate_attribution.py",
    )
    shutil.copy2(
        REPO_ROOT / "backend" / "testing" / "impacted_tests.py",
        tmp_path / "backend" / "testing" / "impacted_tests.py",
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
    # Make backend/app a real regular package in the fixture tree, not a bare
    # directory. Without this, PEP 420 namespace-package resolution lets
    # Python continue down sys.path past the fixture's backend/app and bind
    # `import app.size_limit_policy` to the real checkout's regular package
    # instead, silently reading the live module_size_limits.json and cap-bump
    # ledger rather than the fixture's isolated copies.
    (tmp_path / "backend" / "app" / "__init__.py").write_text("")
    for relative in (
        "static_release_guard.py",
        "verification_receipt.py",
        "guard_suppression_receipt.py",
        "test-backend.sh",
        "backend-test-route.sh",
    ):
        source = REPO_ROOT / "scripts" / relative
        shutil.copy2(source, tmp_path / "scripts" / relative)
    shutil.copy2(
        REPO_ROOT / "backend" / "testing" / "check_size_limits.py",
        tmp_path / "backend" / "testing" / "check_size_limits.py",
    )
    shutil.copy2(
        REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py",
        tmp_path / "backend" / "testing" / "fixgate_attribution.py",
    )
    shutil.copy2(
        REPO_ROOT / "backend" / "testing" / "impacted_tests.py",
        tmp_path / "backend" / "testing" / "impacted_tests.py",
    )
    shutil.copy2(
        REPO_ROOT / "backend" / "testing" / "drift_sentinel_surfaces.json",
        tmp_path / "backend" / "testing" / "drift_sentinel_surfaces.json",
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
<testsuite tests="4" failures="4">$pre_backend$pre_scripts$collect_module$new_roster</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "collect_opaque" ]; then
      cat >"$report" <<XML
<testsuite tests="2" failures="2">$pre_backend$collect_opaque</testsuite>
XML
      exit 1
    fi
    if [ "$state" = "deleted_corpus" ] || [ "$state" = "renamed_corpus" ]; then
      cat >"$report" <<XML
<testsuite tests="1" failures="0"><testcase classname="guard" name="clean" /></testsuite>
XML
      exit 0
    fi
    if [ "$state" = "skipped_corpus" ]; then
      skipped_scripts="<testcase classname=\"...repo.scripts.test_preexisting_roster\" name=\"test_roster_preexisting\"><skipped message=\"test disabled\" /></testcase>"
      cat >"$report" <<XML
<testsuite tests="1" failures="0" skipped="1">$skipped_scripts</testsuite>
XML
      exit 0
    fi
    if [ "$state" = "deleted_base_failure" ]; then
      cat >"$report" <<XML
<testsuite tests="1" failures="1">$pre_scripts</testsuite>
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
    if [ "$state" = "new_node" ] || [ "$state" = "new_node_existing_roster" ]; then
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
    # Current-tree-only sources behind the new-failure shapes: a brand new
    # roster FILE, and a new NODE inside a roster file that already exists in
    # base. Both must resolve against the current tree and be absent from base.
    # Keep them out of unrelated states so the corpus inventory only changes
    # when that fixture is specifically exercising roster drift.
    if current_state in {"new", "new_node", "collect_mixed"}:
        (tmp_path / "scripts" / "test_new_roster.py").write_text(
            "def test_newly_introduced():\n    raise AssertionError('fixture only')\n"
        )
    if current_state in {"new_node", "new_node_existing_roster", "collect_mixed"}:
        with (tmp_path / "backend" / "testing" / "test_existing.py").open("a") as existing:
            existing.write("\n\ndef test_added_here():\n    raise AssertionError('new node')\n")
    with (tmp_path / "scripts" / "static_release_guard.py").open("a") as guard:
        guard.write("# task changes static-guard path\n")
    if current_state == "deleted_corpus":
        (tmp_path / "scripts" / "test_preexisting_roster.py").unlink()
    elif current_state == "renamed_corpus":
        (tmp_path / "scripts" / "test_preexisting_roster.py").rename(
            tmp_path / "scripts" / "test_renamed_roster.py"
        )
    _git(tmp_path, "add", "attribution_state", "scripts/static_release_guard.py", "scripts/backend-test-route.sh")
    _git(tmp_path, "add", "-A", "scripts")
    _git(tmp_path, "commit", "-m", "change static corpus outcome")
    return docker_bin.parent, tmp_path


def test_docker_attribution_repo_isolates_size_limit_policy(tmp_path: Path) -> None:
    """The fixture's backend/app must not be shadowed by the real checkout.

    ``check_size_limits.py`` imports ``app.size_limit_policy`` lazily by
    inserting its own ``backend`` dir at the front of ``sys.path``. That only
    wins the import when ``backend/app`` is a REGULAR package: under PEP 420,
    a bare directory with no ``__init__.py`` is merely a namespace-package
    portion, so Python keeps searching ``sys.path`` and can bind ``app.*`` to
    an unrelated ``app`` package found later — such as the real checkout's
    ``backend/app``, reachable via ``PYTHONPATH`` exactly the way
    ``scripts/test-backend.sh`` sets it (``PYTHONPATH="$(pwd)"`` from the
    real ``backend`` dir) during a live test run. Reproduce that condition
    directly: run the exact import check_size_limits.py performs, from the
    fixture repo, with the real repo's backend dir on PYTHONPATH, and assert
    ``app.size_limit_policy`` resolves to the FIXTURE's own copy.
    """
    _fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "clean")

    probe = (
        "import os, sys\n"
        "backend_dir = os.path.normpath(os.path.join(os.path.dirname(r'"
        + str(repo / "backend" / "testing" / "check_size_limits.py")
        + "'), '..'))\n"
        "if backend_dir not in sys.path:\n"
        "    sys.path.insert(0, backend_dir)\n"
        "import app.size_limit_policy as m\n"
        "print(os.path.realpath(m.__file__))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo,
        env={
            **os.environ,
            # Mirrors what scripts/test-backend.sh sets for a live in-process
            # run: PYTHONPATH pointed at the REAL repo's backend dir, which is
            # where the shadowing "app" regular package lives.
            "PYTHONPATH": str(REPO_ROOT / "backend"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    resolved = os.path.realpath(result.stdout.strip())
    fixture_policy = os.path.realpath(str(repo / "backend" / "app" / "size_limit_policy.py"))
    real_policy = os.path.realpath(str(REPO_ROOT / "backend" / "app" / "size_limit_policy.py"))
    assert resolved != real_policy, (
        "app.size_limit_policy resolved to the REAL repo checkout instead of "
        f"the isolated fixture copy: {output}"
    )
    assert resolved == fixture_policy, (
        f"app.size_limit_policy resolved to an unexpected location {resolved}, "
        f"expected the fixture copy {fixture_policy}: {output}"
    )

    # check_size_limits.py itself now refuses to run at all when its own
    # backend/app isn't a regular package, rather than risk this exact silent
    # shadowing. Exercise BOTH branches of that fail-closed guard directly
    # through the real checker (not a hand probe): it must run clean with the
    # fixture as built (backend/app/__init__.py present) even with the real
    # backend dir polluting PYTHONPATH, and it must refuse to run at all if a
    # future fixture regresses back to a bare backend/app directory.
    checker = repo / "backend" / "testing" / "check_size_limits.py"
    poisoned_env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "backend")}
    # Cap the copied size_limit_policy.py so the assertions below aren't
    # incidentally tripped by the unrelated uncapped-large-file guardrail.
    limits_path = repo / "backend" / "testing" / "module_size_limits.json"
    limits = json.loads(limits_path.read_text())
    limits["backend"]["size_limit_policy.py"] = 10000
    limits_path.write_text(json.dumps(limits) + "\n")

    clean_run = subprocess.run(
        [sys.executable, str(checker), "--sections", "backend"],
        cwd=repo,
        env=poisoned_env,
        capture_output=True,
        text=True,
        check=False,
    )
    clean_output = clean_run.stdout + clean_run.stderr
    assert clean_run.returncode == 0, clean_output
    assert "is not a regular Python package" not in clean_output, clean_output

    (repo / "backend" / "app" / "__init__.py").unlink()
    regressed_run = subprocess.run(
        [sys.executable, str(checker), "--sections", "backend"],
        cwd=repo,
        env=poisoned_env,
        capture_output=True,
        text=True,
        check=False,
    )
    regressed_output = regressed_run.stdout + regressed_run.stderr
    assert regressed_run.returncode != 0, regressed_output
    assert "is not a regular Python package" in regressed_output, regressed_output


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
            "BLUEPRINT_VERIFICATION_RECEIPT": str(_fixture_receipt(repo)),
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
            "BLUEPRINT_VERIFICATION_RECEIPT": str(_fixture_receipt(repo)),
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
    assert len(current) == 1, _offender_detail(current)
    assert set(current) < set(baseline), _offender_detail(baseline)


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


def test_deleted_node_in_unchanged_corpus_file_blocks_a_clean_current_run(
    tmp_path: Path,
) -> None:
    """A source edit must baseline nodes even when file inventory is unchanged."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path,
        "clean",
        base_state="preexisting",
    )
    (repo / "scripts" / "test_preexisting_roster.py").write_text(
        "# test_roster_preexisting deleted by task\n"
    )
    call_log = tmp_path / "deleted-node-clean-calls.log"

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-deleted-node-clean-123",
        call_log=call_log,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "baseline failures disappeared" in output
    assert "scripts/test_preexisting_roster.py::test_roster_preexisting::failure" in output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    assert len(calls) == 2, _offender_detail(calls)
    assert calls[1].replace("base.xml", "current.xml") == calls[0]


def test_deleted_base_failing_corpus_test_blocks_a_clean_current_run(
    tmp_path: Path,
) -> None:
    """Deleting a failing standalone member cannot make a clean task green."""
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "deleted_corpus", base_state="deleted_base_failure"
    )
    call_log = tmp_path / "deleted-corpus-calls.log"

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-deleted-corpus-test-123",
        call_log=call_log,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "baseline failures disappeared" in output
    assert "scripts/test_preexisting_roster.py::test_roster_preexisting::failure" in output
    assert "task-deleted-corpus-test-123" in output
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, _offender_detail(calls)
    assert "scripts/test_preexisting_roster.py" not in calls[0]
    assert calls[1].split("|", 1)[1].replace("base.xml", "current.xml") == calls[0].split("|", 1)[1]


def test_renamed_base_failing_corpus_test_blocks_a_clean_current_run(
    tmp_path: Path,
) -> None:
    """Renaming a failing standalone member preserves the vanished identity."""
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "renamed_corpus", base_state="deleted_base_failure"
    )
    result = _run_attribution_guard(
        repo, fake_bin, task_id="task-renamed-corpus-test-123"
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "scripts/test_preexisting_roster.py::test_roster_preexisting::failure" in output


def test_skipped_base_failing_corpus_test_blocks_with_same_route_identity(
    tmp_path: Path,
) -> None:
    """Turning a failing standalone member into a skip is also baseline erosion."""
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "skipped_corpus", base_state="deleted_base_failure"
    )
    call_log = tmp_path / "skipped-corpus-calls.log"
    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-skipped-corpus-test-123",
        call_log=call_log,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "baseline failures became skipped" in output
    assert "scripts/test_preexisting_roster.py::test_roster_preexisting::failure" in output
    assert len(call_log.read_text(encoding="utf-8").splitlines()) == 2


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
    assert baseline == ["testing/test_existing.py"], _offender_detail(baseline)
    assert "/repo/scripts/test_preexisting_roster.py" not in calls[1]
    assert "no new failures vs base" in output
    assert "[static guard] PASS" in output


def test_deleted_node_with_surviving_failure_runs_full_baseline_and_blocks(
    tmp_path: Path,
) -> None:
    """A surviving failure cannot scope away a deleted baseline sibling node."""

    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path,
        "partial_preexisting",
        base_state="preexisting",
    )
    (repo / "scripts" / "test_preexisting_roster.py").write_text(
        "# test_roster_preexisting deleted by task\n"
    )
    call_log = tmp_path / "deleted-node-surviving-calls.log"

    result = _run_attribution_guard(
        repo,
        fake_bin,
        task_id="task-deleted-node-surviving-123",
        call_log=call_log,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "made base corpus failure(s) disappear" in output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    assert len(calls) == 2, _offender_detail(calls)
    assert calls[1].replace("base.xml", "current.xml") == calls[0]


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


def test_corpus_only_skips_module_size_guard(tmp_path: Path) -> None:
    """The generated backend lane can reuse attribution without size checks."""
    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "clean")
    result = subprocess.run(
        [
            "python3",
            "scripts/static_release_guard.py",
            "--base",
            "automain",
            "--repo-root",
            str(repo),
            "--corpus-only",
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TASK_ID": "task-corpus-only-123",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "module size guard SKIPPED (--corpus-only)" in output
    assert "standalone static corpus OK" in output


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
    """A changed corpus inventory upgrades comparison to the full base run."""
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
    baseline = calls[1].replace("base.xml", "current.xml")
    assert baseline == calls[0]
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
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "new_node_existing_roster"
    )
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
    assert "--junitxml=/static-guard-output/base.xml" in calls[1]
    assert "testing/test_existing.py" in calls[1]
    assert "ERROR: not found" not in output
    # The new node is named; the pre-existing failures in the same file are not.
    assert "standalone static corpus attribution" not in output
    new_line = output.split("new failures:", 1)[1].split("\n")[0]
    assert "test_added_here" in new_line
    assert "test_preexisting" not in new_line
    assert '"task_id": "task-new-node-123"' in new_line


def _fixture_receipt(repo: Path) -> Path:
    """Keep a fixture guard run's sidecar inside the fixture, not in /tmp."""

    return repo / ".git" / "verification_receipt.json"


def _run_attribution_guard(
    repo: Path, fake_bin: Path, *, task_id: str, call_log: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TASK_ID": task_id,
        "BLUEPRINT_VERIFICATION_RECEIPT": str(_fixture_receipt(repo)),
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
    assert len(calls) == 2, _offender_detail(calls)
    assert "testing/test_collect_boom.py" in calls[1], _offender_detail(calls)


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
    assert calls[1].replace("base.xml", "current.xml") == calls[0]
    assert "standalone static corpus attribution" not in output
    new_line = output.split("new failures:", 1)[1].split("\n")[0]
    assert "test_newly_introduced" in new_line
    assert "test_collect_boom" not in new_line
    assert "test_preexisting" not in new_line


def test_static_lane_blocks_on_baseline_failure_disappeared(
    tmp_path: Path,
) -> None:
    """A disappeared baseline failure blocks even with an UNCHANGED harness.

    A failure whose selector cannot be derived forces an UNSCOPED baseline —
    the current tree fails on ``<collection>::conftest::error`` (empty
    classname, no derivable module) plus the ordinary ``test_preexisting``
    failure. The harness itself is untouched, but the unscoped base corpus
    also exposes ``test_roster_preexisting`` and the ``test_collect_boom``
    collection error, neither of which the current tree reports at all.

    Before this guard called the shared ``classify_verification`` classifier,
    static_release_guard.py re-implemented the verdict and only treated a
    disappeared/skipped baseline failure as blocking when ``harness_changed``
    was true — so this exact scenario (harness untouched, two baseline
    failures silently gone) used to pass clean. The other four verification
    lanes block on disappearance unconditionally; the static lane must too.
    """
    fake_bin, repo = _prepare_docker_attribution_repo(
        tmp_path, "collect_opaque", base_state="collect_base"
    )
    call_log = tmp_path / "corpus-calls.log"
    result = _run_attribution_guard(
        repo, fake_bin, task_id="task-collect-opaque-123", call_log=call_log
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "unresolvable failure id(s)" in output
    assert "made base corpus failure(s) disappear" in output
    assert "[static guard] PASS" not in output
    # The two failures that ARE identical to the baseline (the opaque
    # collection error and the ordinary preexisting failure) are still
    # suppressed rather than charged as new — only the disappearance blocks.
    assert "test_newly_introduced" not in output
    calls = [line.split("|", 1)[1] for line in call_log.read_text().splitlines()]
    baseline = [arg for arg in calls[1].split() if not arg.startswith("-")]
    current = [arg for arg in calls[0].split() if not arg.startswith("-")]
    # Unscoped: the baseline runs the same roster as the current tree, including
    # files that did not fail — not the scoped two-file subset.
    assert baseline == current, _offender_detail(baseline)
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


def _static_corpus_lane(repo: Path) -> dict:
    payload = json.loads(_fixture_receipt(repo).read_text(encoding="utf-8"))
    return payload["lanes"]["static-corpus"]


def test_static_corpus_suppressions_land_in_the_shared_verification_receipt(
    tmp_path: Path,
) -> None:
    """The standalone corpus lane's suppressions become durable identities."""

    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "preexisting")
    result = _run_attribution_guard(repo, fake_bin, task_id="task-receipt-preexisting")

    assert result.returncode == 0, result.stdout + result.stderr
    lane = _static_corpus_lane(repo)
    assert lane["lane"] == "static-corpus"
    assert lane["state"] == "preexisting_test_failure"
    assert lane["blocking"] is False
    assert lane["new_failures"] == {}
    # Structured route/test/kind identities, never a rendered log line.
    assert set(lane["suppressed_failures"]) == {
        "backend/testing/test_existing.py::test_preexisting::failure",
        "scripts/test_preexisting_roster.py::test_roster_preexisting::failure",
    }


def test_static_corpus_receipt_records_a_blocking_new_failure(tmp_path: Path) -> None:
    """A blocked corpus lane still names what it introduced and inherited."""

    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "new")
    result = _run_attribution_guard(repo, fake_bin, task_id="task-receipt-new")

    assert result.returncode != 0, result.stdout + result.stderr
    lane = _static_corpus_lane(repo)
    assert lane["state"] == "test_failure"
    assert lane["blocking"] is True
    assert (
        "scripts/test_new_roster.py::test_newly_introduced::failure"
        in lane["new_failures"]
    )


def test_static_corpus_receipt_merges_beside_another_lane(tmp_path: Path) -> None:
    """An earlier lane's block survives the corpus lane's merge."""

    fake_bin, repo = _prepare_docker_attribution_repo(tmp_path, "preexisting")
    receipt_path = _fixture_receipt(repo)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verification_receipt  # noqa: PLC0415

    verification_receipt.record_lane(
        verification_receipt.vitest_lane("agent"),
        state="preexisting_test_failure",
        suppressed_failures={"src/a.test.ts::x::failure": "inherited"},
        receipt_path=receipt_path,
    )

    result = _run_attribution_guard(repo, fake_bin, task_id="task-receipt-merge")

    assert result.returncode == 0, result.stdout + result.stderr
    lanes = json.loads(receipt_path.read_text(encoding="utf-8"))["lanes"]
    assert set(lanes) == {"vitest:agent", "static-corpus"}
    assert lanes["vitest:agent"]["suppressed_failures"] == {
        "src/a.test.ts::x::failure": {"detail": "inherited"}
    }


def test_an_unwritable_receipt_does_not_change_the_static_corpus_verdict(
    tmp_path: Path,
) -> None:
    """Receipt writing stays advisory for the corpus lane too."""

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
            "TASK_ID": "task-receipt-unwritable",
            # A regular file stands where the sidecar's parent directory must be.
            "BLUEPRINT_VERIFICATION_RECEIPT": str(
                repo / "attribution_state" / "receipt.json"
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no new failures vs base" in result.stdout
    assert "could not record lane static-corpus" in result.stderr

# ---------------------------------------------------------------------------
# Cross-lane suppression receipts
# ---------------------------------------------------------------------------


def _suppressing_outcome() -> dict[str, object]:
    """Return a real classify_verification outcome that suppresses a failure."""
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_failures={"testing/test_x.py::test_fails": "boom"},
        baseline_failures={"testing/test_x.py::test_fails": "boom"},
    )
    assert outcome["state"] == "preexisting_test_failure"
    assert outcome["suppressed_failures"], "fixture must actually suppress something"
    return outcome


def _viewport_receipt_commands() -> list[list[str]]:
    """Return the viewport lane's emitter invocations from the generated ./check.

    Only the capture-skipped branch still lives in generated bash: the viewport
    suite now runs through the attributed Vitest guard
    (`--suite admin-ui-viewport`), so a warn-passed inherited viewport failure
    is recorded by that guard under `vitest:admin-ui-viewport` rather than by a
    second hand-rolled baseline compare in bash. The bash call site is read
    straight out of the generator and executed here — a drifted or deleted
    invocation fails this test instead of silently leaving the lane unrecorded.
    """
    source = (REPO_ROOT / "agent" / "src" / "prompts" / "check-helper.ts").read_text(
        encoding="utf-8"
    )
    commands: list[list[str]] = []
    for line in source.splitlines():
        stripped = line.strip()
        if "guard_suppression_receipt.py record" not in stripped:
            continue
        start = stripped.index("python3 scripts/guard_suppression_receipt.py")
        end = stripped.index("|| true", start)
        rendered = (
            stripped[start:end]
            .replace("$VIEWPORT_LABEL", "admin-ui viewport (changed: 1 file(s))")
            .replace("$BASE_REF", "origin/automain")
        )
        commands.append(shlex.split(rendered))
    assert len(commands) == 1, (
        f"expected exactly the capture-skipped viewport branch wired, got {commands}"
    )
    assert "capture_skipped" in commands[0]
    return commands


def test_every_guard_lane_emits_machine_readable_suppression_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All five guard lanes land in one aggregate with parseable identities.

    Before this, only the targeted backend pytest lane left a machine-readable
    record of what it suppressed; the static, vitest, typecheck and viewport
    lanes printed to stdout where nothing consumed them, so a candidate's
    cross-lane suppression set was unrecoverable after the run.
    """
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setenv("BLUEPRINT_GUARD_SUPPRESSION_DIR", str(receipt_dir))
    monkeypatch.setenv("TASK_ID", "left-shift-receipt-test")

    # 1. Backend lane: drive the real guard end to end over a failure present in
    #    both trees, so its suppression is produced by the shipped code path.
    backend_lane_root = tmp_path / "backend-lane"
    backend_lane_root.mkdir()
    backend_outcome = _run_backend_guard_with_baseline(
        backend_lane_root,
        capsys,
        monkeypatch,
        current_junit_text=_FAILING_VERIFICATION_JUNIT,
        current_exit_code=1,
        baseline_junit_text=_FAILING_VERIFICATION_JUNIT,
        baseline_exit_code=1,
    )
    assert backend_outcome["state"] == "preexisting_test_failure"

    # 2-4. Static / vitest / typecheck lanes: drive each guard's own emitter call
    #      site, so the lane identity under test is the one production writes.
    outcome = _suppressing_outcome()
    static_release_guard._record_suppressions(outcome)
    vitest_verification_guard._record_suppressions("admin-ui", outcome)
    typecheck_verification_guard._record_suppressions("admin-ui", outcome)
    # The viewport suite is a Vitest suite of the same guard, so its warn-passed
    # inherited failures land under their own lane identity rather than sharing
    # the unit lane's.
    vitest_verification_guard._record_suppressions("admin-ui-viewport", outcome)

    # 5. Viewport capture-skipped: run the CLI exactly as the generated bash does.
    for command in _viewport_receipt_commands():
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env={**os.environ, "BLUEPRINT_GUARD_SUPPRESSION_DIR": str(receipt_dir)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    aggregate = guard_suppression_receipt.aggregate()
    assert aggregate["version"] == guard_suppression_receipt.SCHEMA_VERSION
    lanes = aggregate["lanes"]
    assert set(lanes) == {
        "backend",
        "static",
        "vitest:admin-ui",
        "vitest:admin-ui-viewport",
        "typecheck:admin-ui",
        "viewport",
    }

    for lane, record in lanes.items():
        buckets = record["buckets"]
        identities = [
            key
            for bucket in ("suppressed", "disappeared", "skipped")
            for key in buckets[bucket]["entries"]
        ]
        assert identities, f"{lane} recorded no suppression identity"
        for key in identities:
            # Machine-readable keys, not prose: a route/test identity, never a
            # sentence and never an empty placeholder.
            assert key.strip() == key and key
            assert "::" in key or key.endswith(".py") or "/" in key

    assert lanes["backend"]["buckets"]["suppressed"]["entries"]
    assert lanes["static"]["state"] == "preexisting_test_failure"
    # The bash-emitted viewport lane is now exactly the capture-skipped branch;
    # a warn-passed inherited viewport failure is `vitest:admin-ui-viewport`.
    assert lanes["viewport"]["state"] == "capture_skipped"
    assert lanes["vitest:admin-ui-viewport"]["buckets"]["suppressed"]["entries"]
    totals = aggregate["totals"]
    assert totals["lanes"] == 6
    assert totals["suppressed"] >= 4
    assert "static" in totals["lanes_with_suppressions"]


def test_suppression_receipt_bounds_payload_and_records_observed_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full tracebacks cannot blow up the receipt, and truncation is visible."""
    monkeypatch.setenv("BLUEPRINT_GUARD_SUPPRESSION_DIR", str(tmp_path / "receipts"))
    oversized = guard_suppression_receipt.MAX_ENTRIES_PER_BUCKET + 7
    suppressed = {
        f"testing/test_big.py::test_{index:03d}": {
            "detail": "T" * (guard_suppression_receipt.MAX_STRING_LENGTH * 4),
            "reason": "same route/test/failure kind failed on baseline",
        }
        for index in range(oversized)
    }
    path = guard_suppression_receipt.record_lane(
        "static", state="preexisting_test_failure", suppressed=suppressed
    )
    assert path is not None
    bucket = json.loads(path.read_text(encoding="utf-8"))["buckets"]["suppressed"]

    assert bucket["observed_count"] == oversized
    assert bucket["recorded_count"] == guard_suppression_receipt.MAX_ENTRIES_PER_BUCKET
    assert bucket["truncated"] is True
    assert len(bucket["entries"]) == guard_suppression_receipt.MAX_ENTRIES_PER_BUCKET
    for entry in bucket["entries"].values():
        assert len(entry["detail"]) == guard_suppression_receipt.MAX_STRING_LENGTH
        assert entry["detail"].endswith("…")
    assert guard_suppression_receipt.aggregate()["totals"]["truncated"] is True


def test_suppression_receipt_write_failure_is_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unwritable receipt directory warns; it never raises into the lane."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("BLUEPRINT_GUARD_SUPPRESSION_DIR", str(blocker))

    assert (
        guard_suppression_receipt.record_lane(
            "static", state="preexisting_test_failure", suppressed={"a::b": "detail"}
        )
        is None
    )
    assert "could not record static suppression receipt" in capsys.readouterr().err
    # And the aggregate over an unusable directory is empty rather than raising.
    assert guard_suppression_receipt.aggregate()["lanes"] == {}


def test_both_check_entry_points_clear_stale_suppression_receipts() -> None:
    """A warm VM's prior task must not donate its suppressions to this run."""
    fallback = (REPO_ROOT / "scripts" / "check-default.sh").read_text(encoding="utf-8")
    generator = (REPO_ROOT / "agent" / "src" / "prompts" / "check-helper.ts").read_text(
        encoding="utf-8"
    )
    for source in (fallback, generator):
        assert 'BLUEPRINT_GUARD_SUPPRESSION_DIR="/tmp/guard_suppression_receipts_' in source
        assert "export BLUEPRINT_GUARD_SUPPRESSION_DIR" in source
        assert 'rm -rf "$BLUEPRINT_GUARD_SUPPRESSION_DIR"' in source
        assert "guard_suppression_receipt.py aggregate" in source


def test_generated_viewport_suppression_uses_plain_cli_not_embedded_python() -> None:
    """Generated bash invokes the emitter as a CLI (CLAUDE.md escaping rule)."""
    generator = (REPO_ROOT / "agent" / "src" / "prompts" / "check-helper.ts").read_text(
        encoding="utf-8"
    )
    for line in generator.splitlines():
        if "guard_suppression_receipt.py" not in line:
            continue
        assert "python3 -c" not in line
        assert "<<" not in line


# ---------------------------------------------------------------------------
# Aggregate -> gate-receipt projection
# ---------------------------------------------------------------------------


def _lane_record(
    lane: str,
    state: str,
    *,
    suppressed: dict[str, object] | None = None,
    disappeared: dict[str, object] | None = None,
    skipped: dict[str, object] | None = None,
    observed_overrides: dict[str, int] | None = None,
    truncated_buckets: tuple[str, ...] = (),
) -> dict[str, object]:
    """Build one aggregate lane record in the shape ``aggregate()`` emits."""
    entries_by_bucket = {
        "suppressed": suppressed or {},
        "disappeared": disappeared or {},
        "skipped": skipped or {},
    }
    overrides = observed_overrides or {}
    return {
        "version": guard_suppression_receipt.SCHEMA_VERSION,
        "lane": lane,
        "task_id": "projection-test",
        "state": state,
        "recorded_at": "2026-08-19T00:00:00+00:00",
        "buckets": {
            bucket: {
                "entries": entries,
                "observed_count": overrides.get(bucket, len(entries)),
                "recorded_count": len(entries),
                "truncated": bucket in truncated_buckets,
            }
            for bucket, entries in entries_by_bucket.items()
        },
    }


def test_gate_projection_forwards_a_lane_whose_only_identity_is_non_suppressed() -> None:
    """The viewport lane's capture-skipped branch records nothing else.

    A suppressed-only projection would drop the lane's entire audit content, so
    all three buckets must survive into the durable receipt.
    """
    aggregate_payload = {
        "version": guard_suppression_receipt.SCHEMA_VERSION,
        "lanes": {
            "viewport": _lane_record(
                "viewport",
                "capture_skipped",
                skipped={
                    "admin-ui/src/components/Foo.tsx": {
                        "detail": "chromium unavailable",
                        "reason": "capture skipped",
                    }
                },
            ),
        },
    }

    projection = guard_suppression_receipt.project_for_gate_receipt(aggregate_payload)

    assert projection["viewport"]["state"] == "capture_skipped"
    assert projection["viewport"]["skipped"] == {
        "admin-ui/src/components/Foo.tsx": "chromium unavailable"
    }
    assert projection["viewport"]["skipped_observed_count"] == 1
    assert projection["viewport"]["skipped_truncated"] is False
    # ``suppressed`` keeps a stable shape for existing consumers even when empty;
    # the empty ``disappeared`` bucket carries no signal and is dropped.
    assert projection["viewport"]["suppressed"] == {}
    assert "disappeared" not in projection["viewport"]


def test_gate_projection_preserves_producer_side_truncation() -> None:
    """A capped bucket can never look complete in the durable receipt."""
    entries = {f"testing/test_big.py::test_{i:03d}": {"detail": "boom"} for i in range(3)}
    aggregate_payload = {
        "lanes": {
            "static": _lane_record(
                "static",
                "preexisting_test_failure",
                suppressed=entries,
                observed_overrides={"suppressed": 31},
                truncated_buckets=("suppressed",),
            ),
        },
    }

    projection = guard_suppression_receipt.project_for_gate_receipt(aggregate_payload)

    assert len(projection["static"]["suppressed"]) == 3
    assert projection["static"]["suppressed_observed_count"] == 31
    assert projection["static"]["suppressed_truncated"] is True


def test_gate_projection_marks_truncation_even_without_a_producer_flag() -> None:
    """observed_count > recorded entries is itself proof the set was clipped."""
    aggregate_payload = {
        "lanes": {
            "vitest:admin-ui": _lane_record(
                "vitest:admin-ui",
                "preexisting_test_failure",
                suppressed={"vitest::flaky::test": {"detail": "inherited"}},
                observed_overrides={"suppressed": 9},
            ),
        },
    }

    projection = guard_suppression_receipt.project_for_gate_receipt(aggregate_payload)

    assert projection["vitest:admin-ui"]["suppressed_observed_count"] == 9
    assert projection["vitest:admin-ui"]["suppressed_truncated"] is True


@pytest.mark.parametrize(
    "aggregate_text",
    [
        pytest.param(None, id="absent"),
        pytest.param("{not json", id="unparseable"),
        pytest.param('{"lanes": "not-a-mapping"}', id="wrong-shape"),
        pytest.param('{"lanes": {}}', id="empty"),
    ],
)
def test_gate_projection_merge_is_fail_open(
    tmp_path: Path, aggregate_text: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed or absent aggregate leaves the gate receipt byte-identical."""
    aggregate_path = tmp_path / "aggregate.json"
    if aggregate_text is not None:
        aggregate_path.write_text(aggregate_text, encoding="utf-8")
    gate_path = tmp_path / "gate.json"
    original = '{"status":"passed","exit_code":0}\n'
    gate_path.write_text(original, encoding="utf-8")

    assert (
        guard_suppression_receipt.merge_into_gate_receipt(aggregate_path, gate_path)
        is False
    )
    assert gate_path.read_text(encoding="utf-8") == original
    if aggregate_text in (None, "{not json"):
        assert "could not project aggregate" in capsys.readouterr().err


def test_gate_projection_merge_writes_guard_suppressions_key(tmp_path: Path) -> None:
    """The happy path adds ``guard_suppressions`` without disturbing the rest."""
    aggregate_path = tmp_path / "aggregate.json"
    aggregate_path.write_text(
        json.dumps(
            {
                "lanes": {
                    "viewport": _lane_record(
                        "viewport",
                        "capture_skipped",
                        skipped={"admin-ui/src/components/Foo.tsx": "chromium unavailable"},
                    ),
                }
            }
        ),
        encoding="utf-8",
    )
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(
        json.dumps({"status": "passed", "attribution_state": "clean"}), encoding="utf-8"
    )

    assert (
        guard_suppression_receipt.merge_into_gate_receipt(aggregate_path, gate_path)
        is True
    )
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["status"] == "passed"
    assert gate["attribution_state"] == "clean"
    assert gate["guard_suppressions"]["viewport"]["skipped"] == {
        "admin-ui/src/components/Foo.tsx": "chromium unavailable"
    }
