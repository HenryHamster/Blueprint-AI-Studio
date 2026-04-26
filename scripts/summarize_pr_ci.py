"""Read-only GitHub PR CI failure summarizer.

Usage:
    python scripts/summarize_pr_ci.py 3269 3267
    python scripts/summarize_pr_ci.py        # all open PRs with failing checks
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from blueprint_api import load_config


DETAILS_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
FAILED_TEST_RE = re.compile(r"(testing/[^\s:]+(?:::[^\s]+)*)\s+FAILED")
UNDEFINED_COLUMN_RE = re.compile(r"UndefinedColumnError[^:\n]*: column ([^\s]+) does not exist")
ERROR_LINE_RE = re.compile(
    r"(AssertionError: .+|RuntimeError: .+|ProgrammingError: .+|sqlalchemy\.exc\.[^\n]+|asyncpg\.exceptions\.[^\n]+)"
)


def _run(cmd: list[str]) -> str:
    completed = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout


def _open_prs(repo: str) -> list[dict[str, Any]]:
    raw = _run([
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,statusCheckRollup",
    ])
    return json.loads(raw)


def _failed_checks(pr: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for check in pr.get("statusCheckRollup") or []:
        if check.get("status") == "COMPLETED" and check.get("conclusion") not in {
            "SUCCESS",
            "NEUTRAL",
            "SKIPPED",
        }:
            checks.append(check)
    return checks


def _summarize_log(log: str) -> dict[str, Any]:
    undefined_columns = sorted(set(UNDEFINED_COLUMN_RE.findall(log)))
    failed_tests = []
    seen_tests = set()
    for match in FAILED_TEST_RE.findall(log):
        if match not in seen_tests:
            seen_tests.add(match)
            failed_tests.append(match)
        if len(failed_tests) >= 20:
            break

    error_lines = []
    seen_errors = set()
    for line in log.splitlines():
        cleaned = line.split("\t")[-1].strip()
        match = ERROR_LINE_RE.search(cleaned)
        if not match:
            continue
        value = match.group(1)
        if value in seen_errors:
            continue
        seen_errors.add(value)
        error_lines.append(value[:220])
        if len(error_lines) >= 8:
            break

    return {
        "undefined_columns": undefined_columns,
        "failed_tests": failed_tests,
        "errors": error_lines,
    }


def main() -> int:
    cfg = load_config()
    repo = cfg["default_repo"]
    requested = {int(arg.lstrip("#")) for arg in sys.argv[1:]}

    prs = _open_prs(repo)
    if requested:
        prs = [pr for pr in prs if pr.get("number") in requested]
    else:
        prs = [pr for pr in prs if _failed_checks(pr)]

    print(f"Repo: {repo}")
    print(f"PRs:  {', '.join('#' + str(pr['number']) for pr in prs) or '(none)'}")
    print()

    for pr in sorted(prs, key=lambda p: p["number"], reverse=True):
        print(f"=== PR #{pr['number']}: {pr.get('title', '')[:120]} ===")
        checks = _failed_checks(pr)
        if not checks:
            print("No failing checks.")
            print()
            continue
        for check in checks:
            name = check.get("name", "?")
            details = check.get("detailsUrl") or ""
            ids = DETAILS_RE.search(details)
            print(f"-- {name}")
            if not ids:
                print(f"   cannot parse details URL: {details}")
                continue
            run_id, job_id = ids.groups()
            try:
                log = _run([
                    "gh",
                    "run",
                    "view",
                    run_id,
                    "--repo",
                    repo,
                    "--job",
                    job_id,
                    "--log-failed",
                ])
            except subprocess.CalledProcessError as exc:
                print(f"   failed to fetch log: {exc}")
                continue
            summary = _summarize_log(log)
            if summary["undefined_columns"]:
                print(f"   undefined columns: {', '.join(summary['undefined_columns'])}")
            if summary["failed_tests"]:
                print("   failed tests:")
                for test in summary["failed_tests"]:
                    print(f"     {test}")
            if summary["errors"]:
                print("   error signatures:")
                for error in summary["errors"]:
                    print(f"     {error}")
            if not any(summary.values()):
                print("   no concise failure signature found")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
