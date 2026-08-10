"""Regression coverage for running static release guards on the task diff."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _prepare_docker_attribution_repo(tmp_path: Path, current_state: str) -> tuple[Path, Path]:
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
    shutil.copy2(
        REPO_ROOT / "backend" / "app" / "size_limit_policy.py",
        tmp_path / "backend" / "app" / "size_limit_policy.py",
    )
    (tmp_path / "backend" / "testing" / "module_size_limits.json").write_text(
        '{"backend": {}, "agent": {}, "frontend": {}}\n'
    )
    (tmp_path / "attribution_state").write_text("preexisting\n")
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
    # Before the fix, the command receives a host /tmp path but that path is
    # not mounted. Model Docker's disposable container filesystem by refusing
    # to create a host report unless the harness supplies the output mount.
    [ -n "$junit" ] && [ -n "$output_root" ] || exit 0
    state="$(cat "$repo_root/attribution_state")"
    report="$output_root/$(basename "$junit")"
    if [ "$state" = "preexisting" ]; then
      cat >"$report" <<'XML'
<testsuite tests="1" failures="1"><testcase classname="guard" name="preexisting"><failure message="preexisting">preexisting</failure></testcase></testsuite>
XML
      exit 1
    fi
    cat >"$report" <<'XML'
<testsuite tests="2" failures="2"><testcase classname="guard" name="preexisting"><failure message="preexisting">preexisting</failure></testcase><testcase classname="guard" name="newly_introduced"><failure message="newly introduced">newly introduced</failure></testcase></testsuite>
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
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "switch", "-c", "task")
    (tmp_path / "attribution_state").write_text(f"{current_state}\n")
    with (tmp_path / "scripts" / "static_release_guard.py").open("a") as guard:
        guard.write("# task changes static-guard path\n")
    _git(tmp_path, "add", "attribution_state", "scripts/static_release_guard.py")
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
