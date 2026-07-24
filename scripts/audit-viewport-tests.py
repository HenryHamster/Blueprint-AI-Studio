#!/usr/bin/env python3
"""Audit viewport test files for quality issues.

Checks each *.viewport.test.tsx file in admin-ui/src/components/ for:
  1. Snapshot assertions (toMatchSnapshot, toMatchInlineSnapshot)
  2. Hardcoded pixel values that could cause flakiness
  3. Timing-dependent assertions (setTimeout without mock, real timers)
  4. Tests with zero substantive assertions
  5. Assertions outside of viewport helper wrappers
  6. DOM queries that assume specific rendering order

Outputs structured JSON report to stdout.
"""

import ast
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

COMPONENTS_DIR = Path("admin-ui/src/components")

# Anti-pattern regexes
ANTI_PATTERNS = {
    "snapshot_assertion": {
        "pattern": r"\.toMatchSnapshot\(|\.toMatchInlineSnapshot\(",
        "label": "Snapshot assertion (flaky on minor rendering changes)",
        "severity": "high",
    },
    "hardcoded_px": {
        "pattern": r"\b\d{3,4}\s*px\b",
        "label": "Hardcoded pixel value in test logic",
        "severity": "medium",
    },
    "raw_settimeout": {
        "pattern": r"(?<!vi\.)setTimeout\s*\(",
        "label": "Raw setTimeout (timing-dependent, not using vi.useFakeTimers)",
        "severity": "high",
    },
    "raw_date_now": {
        "pattern": r"(?<!vi\.)Date\.now\(\)",
        "label": "Raw Date.now() (timing-dependent, not using vi.useFakeTimers)",
        "severity": "medium",
    },
    "raw_performance_now": {
        "pattern": r"performance\.now\(\)",
        "label": "performance.now() without mock",
        "severity": "medium",
    },
    "sleep_in_test": {
        "pattern": r"\bawait\s+(sleep|wait|delay)\s*\(",
        "label": "Raw sleep/wait/delay call (real-time pause)",
        "severity": "high",
    },
    "query_selector_all_index": {
        "pattern": r"(querySelectorAll|getElementsBy\w+)\s*\([^)]*\)\s*\[",
        "label": "Hardcoded index into query result (order-dependent)",
        "severity": "medium",
    },
    "no_assertion_in_test": {
        "pattern": None,  # special case
        "label": "Test with zero assertion calls",
        "severity": "medium",
    },
    "error_boundary_missing": {
        "pattern": None,  # special case
        "label": "Render without error boundary assertion check",
        "severity": "low",
    },
    "dom_get_by_index": {
        "pattern": r"\.children\s*\[|\.childNodes\s*\[",
        "label": "Hardcoded child index access (fragile against DOM order changes)",
        "severity": "low",
    },
    "unmocked_fetch": {
        "pattern": r"(?<!vi\.)fetch\s*\(",
        "label": "Raw fetch() without mock (hits real network)",
        "severity": "high",
    },
}

# Assertion-like function calls (things that actually verify behavior)
ASSERTION_PATTERNS = re.compile(
    r"(expect|assert|should|\.toHave|\\.toBe|\\.toEqual|\\.toContain|\\.toMatch|\\.toThrow)"
    r"|assert\s*\("
)


def scan_file(filepath: Path) -> dict:
    """Scan a single viewport test file for anti-patterns."""
    text = filepath.read_text(encoding="utf-8")
    lines = text.split("\n")
    findings = []
    stats = {
        "total_lines": len(lines),
        "total_tests": 0,
        "tests_with_assertions": 0,
        "assertion_count": 0,
        "anti_pattern_count": 0,
        "uses_expectNoOverflowAt": False,
        "uses_expectFullViewportAt": False,
        "uses_expectNoTextTruncationAt": False,
        "uses_expectMinTouchTargetAt": False,
    }

    # Count test blocks
    test_pattern = re.compile(r"(?:^|\s)(test|it)\s*\(\s*['\"]")
    stats["total_tests"] = len(test_pattern.findall(text))

    # Count assertions
    stats["assertion_count"] = len(ASSERTION_PATTERNS.findall(text))

    # Check for viewport helper usage
    stats["uses_expectNoOverflowAt"] = "expectNoOverflowAt" in text
    stats["uses_expectFullViewportAt"] = "expectFullViewportAt" in text
    stats["uses_expectNoTextTruncationAt"] = "expectNoTextTruncationAt" in text
    stats["uses_expectMinTouchTargetAt"] = "expectMinTouchTargetAt" in text

    for pattern_name, cfg in ANTI_PATTERNS.items():
        if cfg["pattern"] is None:
            continue
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comment lines to avoid false positives on documentation comments.
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue
            matches = re.findall(cfg["pattern"], line)
            if matches:
                findings.append({
                    "pattern": pattern_name,
                    "severity": cfg["severity"],
                    "label": cfg["label"],
                    "line": lineno,
                    "snippet": line.strip()[:120],
                })
                stats["anti_pattern_count"] += len(matches)

    # Check for tests with zero assertions
    # Simple heuristic: count test() blocks that don't contain expect/assert
    # This is approximate - we split on test boundaries
    if stats["total_tests"] > 0 and stats["assertion_count"] == 0:
        findings.append({
            "pattern": "no_assertion_in_test",
            "severity": "medium",
            "label": "Test with zero assertion calls",
            "line": 1,
            "snippet": f"File has {stats['total_tests']} test(s) but 0 assertion calls",
        })

    # Check for hardcoded pixel dimensions in strings (like "375px", "800px")
    hardcoded_dims = re.findall(r'"(\d{3,4})"', text)
    # These are commonly viewport dimensions, which is fine - filter out the
    # standard viewport widths
    suspicious_dims = [d for d in hardcoded_dims
                       if d not in ("375", "768", "1024", "1440", "800", "600", "320", "414", "1200")]

    return {
        "filepath": str(filepath),
        "stats": stats,
        "findings": findings,
        "suspicious_dimensions": suspicious_dims,
    }


def main():
    viewport_files = sorted(COMPONENTS_DIR.glob("*.viewport.test.tsx"))
    all_results = []
    total_anti_patterns = 0
    findings_by_severity = defaultdict(list)

    # Per-component findings
    for vf in viewport_files:
        result = scan_file(vf)
        all_results.append(result)
        total_anti_patterns += result["stats"]["anti_pattern_count"]
        for f in result["findings"]:
            findings_by_severity[f["severity"]].append(f)

    # Summary
    total_tests = sum(r["stats"]["total_tests"] for r in all_results)
    total_assertions = sum(r["stats"]["assertion_count"] for r in all_results)

    report = {
        "summary": {
            "files_scanned": len(viewport_files),
            "total_test_blocks": total_tests,
            "total_assertion_calls": total_assertions,
            "avg_assertions_per_test": round(total_assertions / max(total_tests, 1), 2),
            "total_anti_patterns": total_anti_patterns,
            "findings_by_severity": {
                sev: len(fs) for sev, fs in findings_by_severity.items()
            },
        },
        "files_with_no_viewport_helper": [
            r["filepath"] for r in all_results
            if not (r["stats"]["uses_expectNoOverflowAt"]
                    or r["stats"]["uses_expectFullViewportAt"]
                    or r["stats"]["uses_expectNoTextTruncationAt"]
                    or r["stats"]["uses_expectMinTouchTargetAt"])
        ],
        "files_with_zero_assertions": [
            r["filepath"] for r in all_results
            if r["stats"]["assertion_count"] == 0 and r["stats"]["total_tests"] > 0
        ],
        "findings": [
            {
                "filepath": r["filepath"],
                "assertion_count": r["stats"]["assertion_count"],
                "test_count": r["stats"]["total_tests"],
                "anti_pattern_count": r["stats"]["anti_pattern_count"],
                "uses_viewport_helpers": (
                    r["stats"]["uses_expectNoOverflowAt"]
                    or r["stats"]["uses_expectFullViewportAt"]
                    or r["stats"]["uses_expectNoTextTruncationAt"]
                    or r["stats"]["uses_expectMinTouchTargetAt"]
                ),
                "details": r["findings"],
            }
            for r in all_results
        ],
    }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
