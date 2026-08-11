#!/usr/bin/env python3
"""Attribute a failed targeted backend verification run against its base tree.

The caller has already run the current tree and supplied its JUnit report.  A
base run is deliberately started only when that report contains test failures;
the base selectors are limited to impacted files that exist in the base tree.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from testing.impacted_tests import (  # noqa: E402
    classify_backend_verification,
    parse_junit_failures,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _base_ref(repo: Path, base: str) -> str:
    for candidate in (f"origin/{base}", base):
        if _git(repo, "rev-parse", "--verify", candidate).returncode == 0:
            return candidate
    raise RuntimeError(f"cannot resolve base ref origin/{base} or {base}")


def _extract_base(repo: Path, ref: str, destination: Path) -> None:
    with tempfile.TemporaryFile() as stderr_file:
        archive = subprocess.Popen(
            ["git", "-c", f"safe.directory={repo}", "archive", ref],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert archive.stdout is not None
        with tarfile.open(fileobj=archive.stdout, mode="r|") as bundle:
            bundle.extractall(destination, filter="data")
        return_code = archive.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    if return_code != 0:
        raise RuntimeError(f"git archive {ref} failed: {stderr.strip()}")


def _base_file_selectors(selectors: list[str], base_root: Path) -> list[str]:
    selected: set[str] = set()
    for selector in selectors:
        relative = selector.split("::", 1)[0]
        if (base_root / "backend" / relative).is_file():
            selected.add(relative)
    return sorted(selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--current-junit", type=Path, required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--harness", default="scripts/test-backend.sh")
    parser.add_argument("selectors", nargs="+")
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()

    try:
        current_failures = parse_junit_failures(args.current_junit)
    except (OSError, ET.ParseError) as exc:
        print(json.dumps({"state": "attribution_unavailable", "blocking": True, "error": str(exc)}))
        return 0
    if not current_failures:
        print(json.dumps({"state": "execution_failure", "blocking": True, "new_failures": {}}))
        return 0

    try:
        base_ref = _base_ref(repo, args.base)
        with tempfile.TemporaryDirectory(prefix="blueprint-backend-verification-") as temp_dir:
            temp = Path(temp_dir)
            base_root = temp / "base"
            base_root.mkdir()
            _extract_base(repo, base_ref, base_root)
            base_harness = base_root / args.harness
            if not base_harness.is_file():
                raise RuntimeError(f"base verification harness is missing: {base_harness}")
            base_route = base_root / "scripts" / "backend-test-route.sh"
            current_route = repo / "scripts" / "backend-test-route.sh"
            if current_route.is_file() and base_route.is_file():
                shutil.copy2(current_route, base_route)
                base_route.chmod(base_route.stat().st_mode | 0o111)
            base_selectors = _base_file_selectors(args.selectors, base_root)
            base_junit = temp / "base.xml"
            baseline_failures: dict[str, str]
            if not base_selectors:
                baseline_failures = {}
            else:
                env = {
                    **os.environ,
                    "BLUEPRINT_VERIFICATION_JUNIT": str(base_junit),
                    "TESTMON_NO_FETCH": "1",
                    "USE_TEST_POSTGRES": "true",
                }
                result = subprocess.run(
                    ["bash", str(base_harness), args.route, *base_selectors],
                    cwd=base_root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                try:
                    baseline_failures = parse_junit_failures(base_junit)
                except (OSError, ET.ParseError) as exc:
                    print(
                        json.dumps(
                            {
                                "state": "baseline_unavailable",
                                "blocking": True,
                                "error": str(exc),
                                "base_exit_code": result.returncode,
                            }
                        )
                    )
                    return 0
            outcome = classify_backend_verification(
                runtime_available=True,
                current_exit_code=1,
                current_failures=current_failures,
                baseline_failures=baseline_failures,
                current_root=repo,
                baseline_root=base_root,
            )
            outcome["current_failures"] = current_failures
            outcome["baseline_selectors"] = base_selectors
            print(json.dumps(outcome, sort_keys=True))
            return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "baseline_unavailable", "blocking": True, "error": str(exc)}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
