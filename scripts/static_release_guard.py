#!/usr/bin/env python3
"""Run the database-free release guard corpus against the task diff.

The size checker is baseline-aware.  The remaining static corpus is executed
against both the task tree and an extracted base tree; only newly appearing
pytest failures are attributed to this task.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

STATIC_PATH_PREFIXES = (
    "backend/",
    "check",
    "scripts/test-backend.sh",
    "scripts/static_release_guard.py",
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _base_ref(repo: Path, base: str) -> str | None:
    for candidate in (f"origin/{base}", base):
        if _git(repo, "rev-parse", "--verify", candidate, check=False).returncode == 0:
            return candidate
    return None


def _changed_files(repo: Path, base: str) -> list[str]:
    ref = _base_ref(repo, base)
    if ref is None:
        raise RuntimeError(f"cannot resolve base ref origin/{base} or {base}")
    committed = _git(repo, "diff", "--name-only", f"{ref}...HEAD").stdout.splitlines()
    uncommitted = _git(repo, "diff", "--name-only", "HEAD").stdout.splitlines()
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted({path for path in (*committed, *uncommitted, *untracked) if path})


def _is_static_scope(path: str) -> bool:
    return path.startswith(STATIC_PATH_PREFIXES)


def _extract_base(repo: Path, ref: str, destination: Path) -> None:
    archive = subprocess.Popen(
        ["git", "-c", f"safe.directory={repo}", "archive", ref],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    try:
        with tarfile.open(fileobj=archive.stdout, mode="r|") as bundle:
            bundle.extractall(destination)
    finally:
        stderr = archive.stderr.read().decode("utf-8", errors="replace") if archive.stderr else ""
        return_code = archive.wait()
    if return_code != 0:
        raise RuntimeError(f"git archive {ref} failed ({return_code}): {stderr.strip()}")


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _junit_failures(path: Path, *, label: str) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"{label} JUnit report is missing: {path}")
    if path.stat().st_size == 0:
        raise RuntimeError(f"{label} JUnit report is empty: {path}")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RuntimeError(f"{label} JUnit report is not parseable: {path}: {exc}") from exc
    if not any(element.tag == "testcase" for element in root.iter()):
        raise RuntimeError(f"{label} JUnit report has no testcase records: {path}")
    failures: dict[str, str] = {}
    for case in root.iter("testcase"):
        failure = case.find("failure")
        error = case.find("error")
        detail = failure if failure is not None else error
        if detail is None:
            continue
        key = f"{case.attrib.get('classname', '')}::{case.attrib.get('name', '')}"
        failures[key] = (detail.text or detail.attrib.get("message", "")).strip()
    return failures


def _run_static_corpus(repo: Path, junit: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "BLUEPRINT_STATIC_GUARD_MODE": "1",
        "BLUEPRINT_STATIC_GUARD_JUNIT": str(junit),
        "TESTMON_NO_FETCH": "1",
    }
    return _run(
        ["bash", "scripts/test-backend.sh", "--standalone", "--no-postgres"],
        cwd=repo,
        env=env,
    )


def _run_size_guard(repo: Path, base_root: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            sys.executable,
            "backend/testing/check_size_limits.py",
            "--base-root",
            str(base_root),
        ],
        cwd=repo,
        env=os.environ.copy(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--size-only",
        action="store_true",
        help="run the always-on module-size check without the standalone corpus",
    )
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()

    try:
        changed = _changed_files(repo, args.base)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[static guard] ERROR: {exc}", file=sys.stderr)
        return 2

    scoped = [path for path in changed if _is_static_scope(path)]
    if not scoped:
        print("[static guard] no static release-guard paths changed; skipping")
        return 0

    print("[static guard] changed scope:")
    for path in scoped:
        print(f"  - {path}")

    failures: list[str] = []
    base_ref = _base_ref(repo, args.base)
    assert base_ref is not None
    with tempfile.TemporaryDirectory(prefix="blueprint-static-guard-") as temp_dir:
        base_root = Path(temp_dir) / "base"
        base_root.mkdir()
        try:
            _extract_base(repo, base_ref, base_root)
        except RuntimeError as exc:
            print(f"[static guard] ERROR: {exc}", file=sys.stderr)
            return 2

        size_result = _run_size_guard(repo, base_root)
        if size_result.returncode != 0:
            print("[static guard] module size guard FAILED")
            print(size_result.stdout, end="")
            print(size_result.stderr, end="", file=sys.stderr)
            failures.append("module size guard")
        else:
            print("[static guard] module size guard OK")

        if args.size_only:
            if failures:
                print(
                    f"[static guard] FAIL — {', '.join(failures)}",
                    file=sys.stderr,
                )
                return 1
            print("[static guard] PASS (size-only)")
            return 0

        # Use the task's harness implementation for both trees so the corpus
        # and its static roster are identical. The source under test remains
        # the base tree for the baseline run.
        base_harness = base_root / "scripts" / "test-backend.sh"
        current_harness = repo / "scripts" / "test-backend.sh"
        if not current_harness.is_file() or not base_harness.is_file():
            print("[static guard] ERROR: scripts/test-backend.sh is unavailable", file=sys.stderr)
            return 2
        shutil.copy2(current_harness, base_harness)
        base_harness.chmod(base_harness.stat().st_mode | 0o111)

        base_junit = Path(temp_dir) / "base.xml"
        current_junit = Path(temp_dir) / "current.xml"
        base_result = _run_static_corpus(base_root, base_junit)
        current_result = _run_static_corpus(repo, current_junit)
        try:
            base_failures = _junit_failures(base_junit, label="base")
            current_failures = _junit_failures(current_junit, label="current")
        except RuntimeError as exc:
            print(f"[static guard] ERROR: cannot attribute standalone corpus: {exc}", file=sys.stderr)
            print(base_result.stdout, end="")
            print(base_result.stderr, end="", file=sys.stderr)
            print(current_result.stdout, end="")
            print(current_result.stderr, end="", file=sys.stderr)
            failures.append("standalone static corpus attribution")
        else:
            new_failures = {
                key: detail
                for key, detail in current_failures.items()
                if key not in base_failures or base_failures[key] != detail
            }
            if current_result.returncode != 0 and not new_failures and not current_failures:
                # A harness/import failure has no testcase identity. It is still a
                # real failure, and must not be converted into a green result.
                new_failures = {"<harness>": current_result.stderr or current_result.stdout}
            if new_failures:
                print("[static guard] standalone static corpus FAILED for this diff")
                print(f"[static guard] new failures: {json.dumps(sorted(new_failures))}")
                print(current_result.stdout, end="")
                print(current_result.stderr, end="", file=sys.stderr)
                failures.append("standalone static corpus")
            elif current_result.returncode != 0 and base_result.returncode == 0:
                print("[static guard] standalone static corpus FAILED")
                print(current_result.stdout, end="")
                print(current_result.stderr, end="", file=sys.stderr)
                failures.append("standalone static corpus")
            else:
                print("[static guard] standalone static corpus OK (no new failures vs base)")

    if failures:
        print(f"[static guard] FAIL — {', '.join(failures)}", file=sys.stderr)
        return 1
    print("[static guard] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
