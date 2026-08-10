"""Regression coverage for running static release guards on the task diff."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        ["bash", str(tmp_path / "check")],
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
